"""SLA clock aggregate (P02-US6 / FM-018 / FM-039 seam).

A :class:`SlaClock` is a measurable deadline for a case (or for a
specific task on a case). The first slice ships the minimum needed for
the FM-018 H0 seam (basic SLA clocks, breach detection, audit + timeline
visibility) and the FM-039 H0 refusal seam (legal hold blocks
disposition of the case and its SLA cannot advance the case to a
terminal state while the hold is active — H1 fleshes out the hold
lifecycle).

# Lifecycle

``SlaClock.status`` is one of:

- ``active`` — counting down to ``due_at`` and well ahead of the
  warning threshold. Created by a workflow transition's
  ``start_sla`` action (P02-US1 / T068 / T071) or by an
  application-layer "start clock" use case. Mutable (pause, resume).
  ``"running"`` is accepted as a wire-format alias for ``"active"``
  (older rows and the first-slice H0 scanner used ``running``);
  the aggregate normalizes to ``active``.
- ``warning`` — counting down to ``due_at`` but inside the configured
  warning window (``due_at - warning_at <= remaining < due_at``).
  The scanner writes an ``SlaWarningRaised`` audit row and fires the
  configured ``escalation_on_warning`` action the first time a
  clock crosses the threshold so the worker sees the deadline
  approaching in the timeline before the breach.
- ``paused`` — frozen in place, ``due_at`` is not advancing. Mutable
  (resume, complete). Pause is rare in v1; it lands because the FM-018
  acceptance scenario requires it and because the engine needs a
  way to stop the clock when a case goes on hold or a task is
  delegated.
- ``breached`` — terminal. Set by the SLA scanner when ``now >
  due_at`` and the clock is still ``active`` or ``warning``. The
  breach writes a :data:`SLA_BREACHED` audit row in the same
  transaction (P02-US6) and fires the configured
  ``escalation_on_breach`` action.
- ``completed`` — terminal. Set when the work the clock measured
  finished (task completed, case moved out of the measured state) and
  the breach action did not fire first. A completed clock is the
  success path: the work beat the deadline.

Why two terminal states: a completed clock is "the case moved on
before the deadline" — different operational meaning than a breach
and the timeline must distinguish them so the operator can see
whether the configured escalation fired.

# Why the clock is the aggregate, not a side-effect of the case

The SLA is an independent record so a single case can carry multiple
clocks (one per task, one for the overall case, one for a sub-step).
The clock's lifecycle lives in its own row; the case's lifecycle is
unaffected. Breach actions are configured on the clock and read at
breach time; the workflow author does not encode "what to do on
breach" inline in the transition.

# Idempotency

``SlaClock.mark_breached`` is idempotent: a second call on an
already-breached clock returns ``self`` unchanged (the audit row was
written by the first call; we do not write a duplicate). This is the
domain-level guard against a scanner double-firing on the same clock
between tick boundaries.

``SlaClock.complete`` is also idempotent against an already-``breached``
clock: the operator should not be able to silently "complete" a
breach; completing a clock is a "we made it before the deadline"
operation and a breach means the deadline was missed.

# INTEGRATION_SEAM: ports

``SlaClockRepository`` (in :mod:`app.domain.ports`) is the persistence
Protocol. The SQL adapter lands in T068. The application use case
``app.application.sla.scan_sla`` reads pending clocks, decides
breach, and writes the audit row in the same transaction as the
clock update.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class SlaClockStatus(StrEnum):
    """Lifecycle states for an :class:`SlaClock`.

    ``ACTIVE`` is the canonical "counting down" state; ``RUNNING`` is
    the legacy H0 alias kept so old rows and fixtures keep round-
    tripping through the wire format. The aggregate accepts both and
    normalizes to ``active``.
    """

    ACTIVE = "active"
    RUNNING = "running"
    WARNING = "warning"
    PAUSED = "paused"
    BREACHED = "breached"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class SlaClock:
    """A measurable deadline bound to a case (P02-US6 / FM-018).

    The aggregate is immutable: every state transition returns a new
    instance. The repository persists the new shape. Optimistic
    concurrency lives on the workflow / case side; the clock itself
    is a leaf record whose row is mutated by the scanner and the
    application use case (the scanner is the only writer that
    transitions a running clock to ``breached``).

    Attributes:
        id: Stable clock id.
        tenant_id: Owning tenant (P11-US2 / FM-002).
        case_id: The case the clock measures.
        clock_key: A short, stable, workflow-author-defined key
            (``"review_due"``, ``"decision_due"``). The scanner uses the
            key to group clocks on a case and the timeline uses it to
            render the deadline label.
        started_at: When the clock started. Set once at ``start`` time.
        due_at: The deadline. Pausing freezes ``due_at`` in place;
            resuming recomputes it from the remaining time so a paused
            clock does not "leak" wall-clock time.
        status: Lifecycle status (see :class:`SlaClockStatus`).
        paused_at: When the clock was last paused, or ``None`` when
            running. Used to compute the remaining time on resume.
        breach_action: A free-form action descriptor the scanner
            fires when it breaches. v1 honors two shapes:
            ``{"type": "notify", "queue": "<queue_key>"}`` and
            ``{"type": "create_task", "queue": "<queue_key>",
            "task_type": "escalation"}``. The clock does not interpret
            the action — the application use case dispatches it.
        metadata: Free-form key/value bag the workflow author may use
            to record SLA category, business priority, etc. Never
            embedded in the breach audit row's payload (timeline shows
            the stable subset: ``clock_key``, ``due_at``, ``started_at``,
            ``breached_at``).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    clock_key: str
    started_at: datetime
    due_at: datetime
    status: SlaClockStatus = SlaClockStatus.ACTIVE
    paused_at: datetime | None = None
    # Optional deadline offset at which the clock transitions from
    # ``active`` to ``warning``. ``None`` disables the warning tier
    # (the clock flips straight to ``breached`` past ``due_at``).
    warning_at: datetime | None = None
    breach_action: dict[str, Any] | None = None
    escalation_on_warning: dict[str, Any] | None = None
    escalation_on_breach: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    # ----- factories --------------------------------------------------------

    @staticmethod
    def start(
        *,
        tenant_id: UUID,
        case_id: UUID,
        clock_key: str,
        started_at: datetime,
        due_at: datetime,
        warning_at: datetime | None = None,
        breach_action: dict[str, Any] | None = None,
        escalation_on_warning: dict[str, Any] | None = None,
        escalation_on_breach: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SlaClock:
        """Construct a new running clock with a fixed deadline.

        Why ``started_at`` and ``due_at`` are both supplied rather
        than computed: the workflow engine has the ``Clock`` already;
        passing timestamps in keeps the domain pure (no call to
        ``datetime.now``) and makes the construction deterministic so
        tests do not need a frozen-time fixture to assert
        ``due_at``.

        ``warning_at`` (optional) marks the start of the warning
        window: once ``warning_at <= now < due_at`` the clock is in
        state ``warning`` and the configured
        ``escalation_on_warning`` fires once (idempotently).
        ``warning_at`` must lie in ``(started_at, due_at]``;
        ``None`` disables the warning tier entirely.
        """
        if not clock_key or not clock_key.strip():
            raise ValueError("clock_key is required")
        if due_at <= started_at:
            raise ValueError("due_at must be strictly after started_at")
        if warning_at is not None and (warning_at <= started_at or warning_at > due_at):
            raise ValueError("warning_at must lie in (started_at, due_at]")
        return SlaClock(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            clock_key=clock_key.strip(),
            started_at=started_at,
            due_at=due_at,
            status=SlaClockStatus.ACTIVE,
            paused_at=None,
            warning_at=warning_at,
            breach_action=breach_action,
            escalation_on_warning=escalation_on_warning,
            escalation_on_breach=escalation_on_breach,
            metadata=metadata,
        )

    # ----- transitions ------------------------------------------------------

    def pause(self, *, at: datetime) -> SlaClock:
        """Freeze the clock in place.

        Idempotent: a second call against an already-paused clock
        returns ``self`` unchanged. Calling ``pause`` on a terminal
        clock (breached or completed) raises
        :class:`ValueError` — the clock's deadline has already been
        decided, "pausing" it is meaningless.
        """
        if self.status is SlaClockStatus.PAUSED:
            return self
        if self.status in (SlaClockStatus.BREACHED, SlaClockStatus.COMPLETED):
            raise ValueError(f"cannot pause a {self.status.value} clock")
        return replace(
            self,
            status=SlaClockStatus.PAUSED,
            paused_at=at,
        )

    def resume(self, *, at: datetime) -> SlaClock:
        """Unfreeze the clock and recompute ``due_at`` from the
        remaining budget.

        The remaining budget is ``due_at - paused_at`` (the time the
        clock had left when it was paused). Resuming pushes ``due_at``
        forward by the elapsed pause duration so the worker has the
        same amount of time remaining as when the clock stopped. This
        is the standard "credit the pause" pattern: the clock never
        silently loses time or silently accrues it.

        On resume the status is normalized to ``active`` (a paused
        clock is the only way to land back in the counting-down tier
        after a freeze, regardless of whether the clock was in
        ``active`` or ``warning`` at pause time — the scanner will
        re-evaluate the tier on the next tick).
        """
        if self.status is SlaClockStatus.ACTIVE:
            return self
        if self.status is SlaClockStatus.WARNING:
            # Resume normalizes to ``active`` so the next scan tick
            # can reclassify the clock if it is still inside the
            # warning window. Idempotent against an already-active
            # clock is handled above; the symmetric case is the
            # explicit value.
            return replace(
                self,
                status=SlaClockStatus.ACTIVE,
                paused_at=None,
                due_at=self.due_at + _pause_duration(self, at),
            )
        if self.status is SlaClockStatus.BREACHED:
            raise ValueError("cannot resume a breached clock")
        if self.status is SlaClockStatus.COMPLETED:
            raise ValueError("cannot resume a completed clock")
        if self.paused_at is None:
            # Defensive: a paused clock must carry a paused_at; this
            # is a code invariant, not a user error.
            raise ValueError("paused clock must carry paused_at")
        pause_duration = at - self.paused_at
        if pause_duration.total_seconds() < 0:
            raise ValueError("resume time is before paused_at")
        return replace(
            self,
            status=SlaClockStatus.ACTIVE,
            paused_at=None,
            due_at=self.due_at + pause_duration,
        )

    def complete(self, *, at: datetime) -> SlaClock:
        """Mark the clock completed (work beat the deadline).

        Idempotent: a second call on an already-``completed`` clock
        returns ``self``. Calling ``complete`` on a ``breached`` clock
        is refused — the breach already happened and the timeline
        must show it.
        """
        if self.status is SlaClockStatus.COMPLETED:
            return self
        if self.status is SlaClockStatus.BREACHED:
            raise ValueError(f"cannot complete a breached clock {self.id}")
        if self.status is SlaClockStatus.PAUSED:
            # Treat complete-from-paused as a clean close: drop the
            # paused_at so the resulting row is a tidy terminal
            # record. The deadline in effect is the one at pause
            # time — we do not "credit" the worker for time the
            # clock was not running.
            return replace(
                self,
                status=SlaClockStatus.COMPLETED,
                paused_at=None,
            )
        _ = at
        return replace(self, status=SlaClockStatus.COMPLETED)

    def mark_breached(self, *, at: datetime) -> SlaClock:
        """Flip the clock to ``breached`` (FM-018 / P02-US6).

        Idempotent: a second call on an already-breached clock
        returns ``self`` unchanged so the scanner can be retried
        without doubling the audit row. Calling ``mark_breached`` on a
        clock that has already been completed or paused is refused —
        the scanner only fires on active / warning clocks that have
        passed their deadline.
        """
        if self.status is SlaClockStatus.BREACHED:
            return self
        if self.status is SlaClockStatus.COMPLETED:
            raise ValueError(f"cannot breach a completed clock {self.id}")
        if self.status is SlaClockStatus.PAUSED:
            # A paused clock is not eligible for breach. The scanner
            # only inspects counting clocks; this branch is defense in
            # depth in case the scanner is bypassed.
            raise ValueError(f"cannot breach a paused clock {self.id}")
        _ = at
        return replace(self, status=SlaClockStatus.BREACHED)

    def mark_warning(self, *, at: datetime) -> SlaClock:
        """Flip the clock to ``warning`` (P02-US6 / FM-018 warning tier).

        Idempotent: a second call on an already-warning clock returns
        ``self`` unchanged so the scanner can be retried without
        doubling the audit row. The aggregate accepts both ``active``
        and the legacy ``running`` alias as the source state.
        Calling ``mark_warning`` on a terminal or paused clock is
        refused — the warning tier only applies to clocks still
        counting down.
        """
        if self.status is SlaClockStatus.WARNING:
            return self
        if self.status in (
            SlaClockStatus.BREACHED,
            SlaClockStatus.COMPLETED,
            SlaClockStatus.PAUSED,
        ):
            raise ValueError(f"cannot warn a {self.status.value} clock {self.id}")
        _ = at
        return replace(self, status=SlaClockStatus.WARNING)

    # ----- queries ----------------------------------------------------------

    def is_counting(self) -> bool:
        """Return True iff the clock is actively counting down.

        The warning tier is a counting state — the clock is still
        moving toward ``due_at``; the tier is purely informational and
        drives the ``SlaWarningRaised`` audit row + warning
        escalation. Paused and terminal clocks do not count.
        """
        return self.status in (
            SlaClockStatus.ACTIVE,
            SlaClockStatus.RUNNING,
            SlaClockStatus.WARNING,
        )

    def is_overdue(self, *, at: datetime) -> bool:
        """Return True iff the clock is counting and ``at > due_at``.

        Paused and terminal clocks are never overdue: a paused clock
        is by definition not counting, and a breached / completed
        clock's deadline is already decided. The scanner only acts on
        clocks where ``is_overdue`` returns True.
        """
        if not self.is_counting():
            return False
        return at > self.due_at

    def is_in_warning_window(self, *, at: datetime) -> bool:
        """Return True iff the clock is in its warning window.

        The window is ``[warning_at, due_at)``. When ``warning_at``
        is unset the warning tier is disabled and the method
        always returns False; the scanner skips the warning pass
        entirely and the clock flips straight to ``breached`` at
        ``due_at``. A clock in ``warning`` already returns True so
        the scanner can detect "still warning" without comparing
        timestamps twice.
        """
        if not self.is_counting():
            return False
        if self.status is SlaClockStatus.WARNING:
            return True
        if self.warning_at is None:
            return False
        return self.warning_at <= at < self.due_at


def _pause_duration(clock: SlaClock, at: datetime) -> timedelta:
    """Return the elapsed pause duration for ``clock`` at ``at``.

    Defensive helper: a paused clock always carries ``paused_at``,
    and the caller (``resume``) checks this before invoking. Kept
    inline so ``resume`` stays a single, readable method.
    """
    if clock.paused_at is None:
        raise ValueError("paused clock must carry paused_at")
    duration = at - clock.paused_at
    if duration.total_seconds() < 0:
        raise ValueError("resume time is before paused_at")
    return duration


__all__ = [
    "SlaClock",
    "SlaClockStatus",
]
