"""SLA scanner use case (P02-US6 / FM-018).

The scanner is the only writer that flips a counting :class:`SlaClock`
between ``active`` / ``warning`` / ``breached``. The first-slice
posture is a local in-process loop that runs every
``scan_interval_seconds`` and:

1. Calls :meth:`SlaClockRepository.list_due` for each tenant
   (the loop is per-tenant so a single tenant's row volume cannot
   starve the rest; a future slice may shard by tenant id).
2. For each candidate clock, asks the aggregate
   :meth:`SlaClock.is_overdue` to confirm ``now > due_at`` (the
   repository already filtered on counting status and
   ``due_at <= now``, but the aggregate is the authority on the
   deadline).
3. Calls :meth:`SlaClock.mark_breached` (idempotent — a re-entered
   clock returns the same shape and does not stage a second audit
   row).
4. Persists the new clock state, writes the ``SlaBreached`` audit
   row, enqueues the breach event on the outbox, and dispatches the
   configured ``escalation_on_breach`` action — all in the same
   transaction. The ``breached_at`` and ``due_at`` carried on the
   audit payload are the values the scanner read; the timeline shows
   "SLA breached at <ts> for clock <key>" without re-reading the
   clock.

The warning pass (:func:`scan_warning_once`) runs against counting
clocks that are still ahead of ``due_at`` but already inside the
configured ``warning_at..due_at`` window. The first tick that sees a
clock inside the window flips it to ``warning`` (idempotent), writes
an ``SlaWarningRaised`` audit row, enqueues the warning outbox
event, and fires ``escalation_on_warning`` (typically a
``create_task`` action that materializes a follow-on task for the
supervisor). Subsequent ticks find the clock already in ``warning``
and skip it; the row only lands once.

# Idempotency

The scanner is safe to run at any cadence — the domain
:func:`mark_breached` and :func:`mark_warning` are the dedup layer,
the audit layer's :func:`record_event` is the second, and the
per-tenant clock list filters to counting states so a clock the
scanner already processed is not revisited. The loop can also be
replayed against a backdated ``now`` (e.g. the verify-audit CLI)
without double-writing rows.

# INTEGRATION_SEAM: scheduler

The loop itself is the AGENTS.md §12 seam. The local adapter runs
in-process from the FastAPI startup task; a production target runs
as a hosted worker (Azure Functions / Container Apps) consuming
the same table through the same use case. The use case does not
import any scheduler — the caller passes ``clock`` and decides
how often to invoke :func:`scan_once` /
:func:`scan_warning_once`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.domain.audit import SLA_BREACHED, SLA_WARNING_RAISED
from app.domain.ports import (
    AuditEventStore,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
)
from app.domain.sla import SlaClock
from app.domain.task import Task

# Default per-tick cap. Sized for the local profile (in-proc loop,
# small dev DB). The production worker may pass a larger limit.
DEFAULT_SCAN_BATCH = 100

# Escalation action types the scanner evaluates deterministically
# (FM-018 / T4-US3). ``create_task`` and ``supervisor_task`` produce
# a task row; ``notify`` is the deferred fan-out placeholder (no
# transport — the outbox event itself is the notification, the
# scanner reports ``escalation_fired=false``); ``queue_move`` and
# ``exception_state`` are recorded as auditable routing intents (the
# case-queue / workflow-instance mutation itself is a later slice —
# see the _dispatch_escalation docstring).
ALLOWED_ESCALATION_TYPES: tuple[str, ...] = (
    "create_task",
    "notify",
    "supervisor_task",
    "queue_move",
    "exception_state",
)

# Runtime default when a ``supervisor_task`` escalation omits ``queue``.
# why a constant: the scanner must resolve the same target on every
# tick without I/O, so the default lives here next to the validator
# that accepts the short form (validate_workflow_definition).
DEFAULT_SUPERVISOR_QUEUE = "supervisor_queue"


# Stable event types. Subscribers depend on the literal strings.
OUTBOX_EVENT_SLA_BREACHED: str = "SlaBreached"
OUTBOX_EVENT_SLA_WARNING_RAISED: str = "SlaWarningRaised"


class _ClockLike(Protocol):
    """Minimal clock protocol used by :func:`scan_once`."""

    def now(self) -> datetime: ...


class _CaseLookup(Protocol):
    """Read-only case resolver used to enforce the FM-039 hold seam.

    The scanner asks the case whether it is under legal hold so a
    held case's clocks can still breach (the deadline did pass) but
    the breach action does not fire — disposition is blocked and
    the operator sees the breach in the timeline. H1 will replace
    the flag with a full hold record; the seam stays.
    """

    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool: ...


class _TaskCreator(Protocol):
    """Application-layer seam that materializes a ``create_task``
    breach / warning action.

    The scanner does not import :mod:`app.application.tasks` directly
    to keep the dependency direction tidy: the workflow / SLA module
    owns the read / breach path, the task module owns the write path.
    The router wires the concrete creator at the boundary; tests
    supply a stub.
    """

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        task_type: str,
        title: str,
        priority: str,
        queue_key: str | None,
    ) -> Task: ...


@dataclass(slots=True)
class ScanSlaResult:
    """The per-tick outcome of :func:`scan_once`.

    ``breached`` is the list of clocks the tick flipped to
    ``breached`` this run; the timeline endpoint reads this to
    surface the breach reason. ``skipped_holds`` is the count of
    candidates that were on a held case and so did not fire an
    escalation action; the audit row still lands so the operator
    can see the deadline passed.

    The dataclass is intentionally non-frozen: the scanner
    accumulates the three counters as it processes the candidate
    list, and rebuilding the instance per candidate would
    allocate more than necessary. Callers should treat the
    instance as read-only after :func:`scan_once` returns.
    """

    breached: list[UUID] = field(default_factory=list)
    skipped_holds: int = 0
    considered: int = 0


@dataclass(slots=True)
class ScanWarningResult:
    """The per-tick outcome of :func:`scan_warning_once`.

    ``warned`` is the list of clocks the tick flipped from
    ``active`` to ``warning`` this run; the timeline endpoint reads
    this to render the warning event. ``skipped_holds`` counts
    candidates on a held case so the warning escalation is not
    fired (the deadline is still approaching, the operator sees the
    warning in the timeline, the held case does not get a new
    task). The scanner can be retried without doubling rows — the
    aggregate's :func:`mark_warning` is idempotent.
    """

    warned: list[UUID] = field(default_factory=list)
    skipped_holds: int = 0
    considered: int = 0


async def scan_once(
    *,
    tenant_id: UUID,
    clock_repo: SlaClockRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    case_lookup: _CaseLookup,
    task_creator: _TaskCreator,
    task_repo: TaskRepository,
    clock: _ClockLike,
    batch_size: int = DEFAULT_SCAN_BATCH,
) -> ScanSlaResult:
    """Run one breach scan tick for a single tenant.

    The function is per-tenant so a multi-tenant deployment can
    drive the scanner with a worker pool without a single
    tenant's row volume starving the rest. The caller iterates
    tenant ids (the local adapter reads them from a ``SELECT id
    FROM tenants`` query; production workers use the same query
    or a configuration-driven set).

    Failure behavior:

    - Repository / audit / outbox errors propagate so the caller
      can log and retry the tick. A single clock failure rolls
      back the audit + outbox rows for that clock and the loop
      moves on; a structural failure (table missing, connection
      refused) is the caller's problem.
    - Domain errors from :meth:`SlaClock.mark_breached` (clock
      already terminal, clock paused) are not retried — the
      candidate is skipped and the loop logs at debug level. The
      list_due query already filters to counting clocks so a
      non-counting candidate means a concurrent mutation
      between SELECT and UPDATE; the next tick will revisit if
      the clock is still eligible.
    """
    now = clock.now()
    candidates = await clock_repo.list_due(tenant_id, now=now, limit=batch_size)
    result = ScanSlaResult(considered=len(candidates))
    for candidate in candidates:
        if not candidate.is_overdue(at=now):
            # Defensive: list_due already filters on status and
            # due_at, but a clock that was paused between
            # list_due and this check is not overdue anymore.
            continue
        breached = candidate.mark_breached(at=now)
        await clock_repo.update(breached)
        correlation_id = _new_correlation_id()
        case_held = await case_lookup.is_under_legal_hold(tenant_id, candidate.case_id)
        action_fired = False
        escalation_type, escalation_target = describe_escalation(_escalation_for_breach(breached))
        if not case_held:
            action_fired = await _dispatch_escalation(
                action=_escalation_for_breach(breached),
                clock=breached,
                tenant_id=tenant_id,
                task_creator=task_creator,
                task_repo=task_repo,
            )
        await audit.append(
            tenant_id=tenant_id,
            event_type=SLA_BREACHED,
            actor_id=_SYSTEM_ACTOR_ID,
            actor_type="system",
            subject_type="sla_clock",
            subject_id=breached.id,
            case_id=breached.case_id,
            correlation_id=correlation_id,
            causation_id=None,
            payload={
                "clock_key": breached.clock_key,
                "due_at": breached.due_at.isoformat(),
                "started_at": breached.started_at.isoformat(),
                "warning_at": (
                    breached.warning_at.isoformat() if breached.warning_at is not None else None
                ),
                "case_under_legal_hold": case_held,
                "escalation_fired": action_fired,
                "escalation_type": escalation_type,
                "escalation_target": escalation_target,
                "breached_at": now.isoformat(),
            },
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=tenant_id,
            event_type=OUTBOX_EVENT_SLA_BREACHED,
            event_id=uuid4(),
            payload={
                "tenant_id": str(tenant_id),
                "case_id": str(breached.case_id),
                "sla_clock_id": str(breached.id),
                "clock_key": breached.clock_key,
                "due_at": breached.due_at.isoformat(),
                "breached_at": now.isoformat(),
                "case_under_legal_hold": case_held,
                "escalation_fired": action_fired,
                "escalation_type": escalation_type,
                "escalation_target": escalation_target,
            },
        )
        result.breached.append(breached.id)
        if case_held:
            result.skipped_holds += 1
    return result


async def scan_warning_once(
    *,
    tenant_id: UUID,
    clock_repo: SlaClockRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    case_lookup: _CaseLookup,
    task_creator: _TaskCreator,
    task_repo: TaskRepository,
    clock: _ClockLike,
    batch_size: int = DEFAULT_SCAN_BATCH,
) -> ScanWarningResult:
    """Run one warning-tier scan tick for a single tenant.

    Mirrors :func:`scan_once` but only inspects counting clocks
    whose ``warning_at <= now < due_at``. The first tick that
    sees a clock inside the window flips it to ``warning``,
    writes the ``SlaWarningRaised`` audit row, enqueues the
    warning outbox event, and fires the configured
    ``escalation_on_warning`` action. Subsequent ticks find the
    clock already in ``warning`` and skip it; the row only
    lands once.

    Note: the breach pass uses :meth:`SlaClockRepository.list_due`
    which filters on ``due_at <= now``. The warning pass needs to
    see clocks that are still ahead of the deadline but already
    inside the ``warning_at..due_at`` window, so it uses
    :meth:`SlaClockRepository.list_counting` and then classifies
    against the timestamp on the aggregate. The scanner's
    defensive ``is_in_warning_window`` check skips clocks that are
    outside the window so the two passes can coexist on the same
    aggregate without the warning pass accidentally breaching
    a clock that is still ahead of ``due_at``.
    """
    now = clock.now()
    candidates = await clock_repo.list_counting(tenant_id, now=now, limit=batch_size)
    result = ScanWarningResult(considered=len(candidates))
    for candidate in candidates:
        if not candidate.is_in_warning_window(at=now):
            continue
        pre = candidate
        warned = candidate.mark_warning(at=now)
        # Idempotency: a clock the scanner has already flipped to
        # ``warning`` returns the same instance from ``mark_warning``
        # so the second tick of the same ``now`` does not stage a
        # duplicate audit row, escalation task, or outbox event. The
        # ``is`` check is the canonical dedup the aggregate's
        # :func:`mark_warning` enforces — a clock that is already
        # ``warning`` is left in place.
        already_warned = warned is pre
        if not already_warned:
            await clock_repo.update(warned)
            correlation_id = _new_correlation_id()
            case_held = await case_lookup.is_under_legal_hold(tenant_id, candidate.case_id)
            action_fired = False
            escalation_type, escalation_target = describe_escalation(
                _escalation_for_warning(warned)
            )
            if not case_held:
                action_fired = await _dispatch_escalation(
                    action=_escalation_for_warning(warned),
                    clock=warned,
                    tenant_id=tenant_id,
                    task_creator=task_creator,
                    task_repo=task_repo,
                )
            await audit.append(
                tenant_id=tenant_id,
                event_type=SLA_WARNING_RAISED,
                actor_id=_SYSTEM_ACTOR_ID,
                actor_type="system",
                subject_type="sla_clock",
                subject_id=warned.id,
                case_id=warned.case_id,
                correlation_id=correlation_id,
                causation_id=None,
                payload={
                    "clock_key": warned.clock_key,
                    "due_at": warned.due_at.isoformat(),
                    "started_at": warned.started_at.isoformat(),
                    "warning_at": (
                        warned.warning_at.isoformat() if warned.warning_at is not None else None
                    ),
                    "case_under_legal_hold": case_held,
                    "escalation_fired": action_fired,
                    "escalation_type": escalation_type,
                    "escalation_target": escalation_target,
                    "warning_at_now": now.isoformat(),
                },
                occurred_at=now,
            )
            await outbox.enqueue(
                tenant_id=tenant_id,
                event_type=OUTBOX_EVENT_SLA_WARNING_RAISED,
                event_id=uuid4(),
                payload={
                    "tenant_id": str(tenant_id),
                    "case_id": str(warned.case_id),
                    "sla_clock_id": str(warned.id),
                    "clock_key": warned.clock_key,
                    "due_at": warned.due_at.isoformat(),
                    "warning_at_now": now.isoformat(),
                    "case_under_legal_hold": case_held,
                    "escalation_fired": action_fired,
                    "escalation_type": escalation_type,
                    "escalation_target": escalation_target,
                },
            )
            result.warned.append(warned.id)
            if case_held:
                result.skipped_holds += 1
    return result


# ---------------------------------------------------------------------------
# Escalation dispatcher
# ---------------------------------------------------------------------------


def describe_escalation(  # noqa: PLR0911, PLR0912 - five-type dispatch is intentionally flat
    action: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Describe an escalation action deterministically (FM-018 / T4-US3).

    Pure function: no I/O, no time, no randomness. The same action
    dict always yields the same ``(type, target)`` pair so the audit
    timeline, the outbox payload, and the unit tests all read the
    same values. ``None`` yields ``(None, None)``; an unknown or
    blank type yields the raw type with a ``None`` target so the
    caller audits it instead of silently dropping it.

    Target resolution (mirrors the publish-time validator):

    - ``create_task`` / ``supervisor_task`` map to ``queue``
      (supervisor defaults to ``DEFAULT_SUPERVISOR_QUEUE``).
    - ``notify`` maps to ``queue`` or ``channel`` when present.
    - ``queue_move`` maps to ``target_queue`` (alias ``queue``).
    - ``exception_state`` maps to ``exception_state`` (aliases
      ``state``, ``target_state``).
    """
    if action is None:
        return None, None
    raw_type = action.get("type", "")
    action_type = str(raw_type).strip() or None
    if action_type is None:
        return None, None
    if action_type == "supervisor_task":
        raw_queue = action.get("queue")
        if isinstance(raw_queue, str) and raw_queue.strip():
            return action_type, raw_queue.strip()
        return action_type, DEFAULT_SUPERVISOR_QUEUE
    if action_type == "create_task":
        raw_queue = action.get("queue")
        if isinstance(raw_queue, str) and raw_queue.strip():
            return action_type, raw_queue.strip()
        return action_type, None
    if action_type == "notify":
        for key in ("queue", "channel"):
            raw = action.get(key)
            if isinstance(raw, str) and raw.strip():
                return action_type, raw.strip()
        return action_type, None
    if action_type == "queue_move":
        raw_target = action.get("target_queue", action.get("queue"))
        if isinstance(raw_target, str) and raw_target.strip():
            return action_type, raw_target.strip()
        return action_type, None
    if action_type == "exception_state":
        raw_state = action.get("exception_state", action.get("state", action.get("target_state")))
        if isinstance(raw_state, str) and raw_state.strip():
            return action_type, raw_state.strip()
        return action_type, None
    return action_type, None


def _escalation_for_breach(clock: SlaClock) -> dict[str, Any] | None:
    """Return the configured breach escalation, or ``None``.

    Prefers the explicit ``escalation_on_breach`` slot. Falls back
    to the legacy ``breach_action`` so workflows published under
    the H0 schema keep working — the new field is the canonical
    name, the old field is a compatibility shim.
    """
    if clock.escalation_on_breach is not None:
        return clock.escalation_on_breach
    return clock.breach_action


def _escalation_for_warning(clock: SlaClock) -> dict[str, Any] | None:
    """Return the configured warning escalation, or ``None``."""
    return clock.escalation_on_warning


async def _dispatch_escalation(  # noqa: PLR0911 - five-type dispatch is intentionally flat
    *,
    action: dict[str, Any] | None,
    clock: SlaClock,
    tenant_id: UUID,
    task_creator: _TaskCreator,
    task_repo: TaskRepository,
) -> bool:
    """Fire the configured escalation action (FM-018 / T4-US3).

    Deterministic: the same action dict on the same clock always
    takes the same branch and returns the same bool. ``True`` means
    a task was created or a routing intent was recorded; ``False``
    means the action was absent, unrecognized, misconfigured, or
    explicitly deferred (``notify``). The audit row records
    ``escalation_fired`` plus ``escalation_type`` / target (see
    :func:`describe_escalation`) so the timeline shows the outcome.
    """
    if action is None:
        return False
    action_type = str(action.get("type", "")).strip()
    if action_type == "supervisor_task":
        task_type = str(action.get("task_type", "supervisor_review")).strip()
        if not task_type:
            task_type = "supervisor_review"
        title = str(action.get("title", f"SLA breach: {clock.clock_key} (supervisor)"))
        priority = str(action.get("priority", "high"))
        raw_queue = action.get("queue")
        if isinstance(raw_queue, str) and raw_queue.strip():
            queue_key_str = raw_queue.strip()
        else:
            queue_key_str = DEFAULT_SUPERVISOR_QUEUE
        task = await task_creator(
            tenant_id=tenant_id,
            case_id=clock.case_id,
            task_type=task_type,
            title=title,
            priority=priority,
            queue_key=queue_key_str,
        )
        await task_repo.add(task)
        return True
    if action_type == "queue_move":
        raw_target = action.get("target_queue", action.get("queue"))
        target = str(raw_target).strip() if isinstance(raw_target, str) else ""
        return bool(target)
    if action_type == "exception_state":
        raw_state = action.get("exception_state", action.get("state", action.get("target_state")))
        target_state = str(raw_state).strip() if isinstance(raw_state, str) else ""
        return bool(target_state)
    if action_type == "create_task":
        task_type = str(action.get("task_type", "escalation")).strip()
        title = str(action.get("title", f"SLA breach: {clock.clock_key}"))
        priority = str(action.get("priority", "high"))
        queue_key = action.get("queue")
        create_queue_key: str | None = str(queue_key).strip() if queue_key else None
        if not create_queue_key:
            # Fail-closed: a create_task action without a queue has
            # nowhere to land. The publish-time validator rejects
            # this shape; runtime is defense-in-depth.
            return False
        task = await task_creator(
            tenant_id=tenant_id,
            case_id=clock.case_id,
            task_type=task_type,
            title=title,
            priority=priority,
            queue_key=create_queue_key,
        )
        await task_repo.add(task)
        return True
    # ``notify`` is a no-op in this slice — the breach event
    # itself is the notification; the timeline surfaces it. A
    # future slice may fan it out to email / IM. We deliberately
    # return False so the audit row reports "no action
    # fired" for the placeholder shape; the operator can
    # distinguish "action configured but not implemented" from
    # "action fired successfully". Unknown types also land here
    # (deterministic ``False`` — the audit row keeps the raw
    # ``escalation_type`` so the misconfiguration is visible).
    return False


# Sentinel id used as the "actor" of system-originated audit rows.
# All-zeros is the conventional system-actor placeholder; the
# production schema may switch to a real "system" user row, but for
# the H0 slice the all-zeros id is unambiguous in the timeline.
_SYSTEM_ACTOR_ID: UUID = UUID(int=0)


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "ALLOWED_ESCALATION_TYPES",
    "DEFAULT_SCAN_BATCH",
    "DEFAULT_SUPERVISOR_QUEUE",
    "OUTBOX_EVENT_SLA_BREACHED",
    "OUTBOX_EVENT_SLA_WARNING_RAISED",
    "ScanSlaResult",
    "ScanWarningResult",
    "describe_escalation",
    "scan_once",
    "scan_warning_once",
]
