"""Complete a task idempotently (P02-US1 / FM-014 / FR-011).

Completing a task is the smallest observable behavior of the workflow
engine. Two layers of idempotency cooperate:

- **Domain**: :meth:`Task.complete` returns the existing aggregate
  unchanged on a second call against an already-``completed`` task, so
  within-process duplicates cannot stage a second audit row.
- **HTTP**: the ``Idempotency-Key`` middleware
  (``app.interfaces.http.idempotency``) caches the response so a
  network-retry after a 200 returns the same wire shape. A duplicate
  completion with any idempotency key is a no-op at the domain layer
  and does not append a second completion event.

The use case flow (mirror of :mod:`app.application.cases.create_case`):

1. **Authorize** the actor against ``Action.TASK_COMPLETE`` on the
   task's case (P11-US2 / FM-002). Cross-tenant actors are denied
   unconditionally — the repository never matches a task from another
   tenant so the policy is belt-and-suspenders on the actor's own
   tenant_id.
2. **Resolve the task**, tenant-scoped. A missing or cross-tenant id
   surfaces as :class:`NotFound`; the two are intentionally
   indistinguishable so the API never confirms cross-tenant existence
   (Constitution V).
3. **Complete** the task via :meth:`Task.complete`. The aggregate
   bumps ``version_token`` on the first call; a duplicate call against
   an already-completed task returns the same shape.
4. **Persist task + audit + outbox** in the same DB transaction:
   ``tasks`` UPDATE gated by the version token, ``audit_events`` with
   ``TaskCompleted``, ``outbox`` with a minimal ``TaskCompleted`` event.
   No partial commit; the boundary commits once.
5. **Optional follow-on transition** — if the command carries
   ``trigger_transition_key``, the use case resolves the case's
   workflow instance and calls :func:`execute_transition.handle` in the
   same transaction. The transition either succeeds (case moves to
   the next state, follow-on tasks are created) or raises — the
   surrounding transaction rolls back so the task is not committed
   ``completed`` without its paired transition.

# INTEGRATION_SEAM: HTTP entry

``POST /api/v1/tasks/{id}/complete`` is the canonical entry point.
The body carries ``{"outcome": {...}}``; the response is the
post-completion ``TaskView``. The endpoint requires
``Idempotency-Key`` (FR-011 / AGENTS.md §10) — the middleware caches
the response so a network-retry replays it verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import TASK_COMPLETED
from app.domain.errors import NotFound
from app.domain.ports import AuditEventStore, OutboxWriter, TaskRepository
from app.domain.task import Task

SUBJECT_TYPE_TASK = "task"


class _ClockLike(Protocol):
    """Minimal clock protocol used by :func:`handle`.

    Mirrors the same pattern as :mod:`app.application.cases.edit_case`:
    a local Protocol keeps the use case free of FastAPI imports.
    """

    def now(self) -> datetime: ...


class _CaseLookup(Protocol):
    """Read-only case resolver — needed to construct the policy resource."""

    async def get(self, tenant_id: UUID, case_id: UUID) -> Any | None: ...


class _TransitionRunner(Protocol):
    """The optional follow-on transition runner.

    Injected as a Protocol so :func:`handle` does not import
    ``execute_transition`` directly (that would invert the dependency
    direction — the workflow module owns the runner). The router wires
    the concrete ``execute_transition.handle`` at the boundary; tests
    supply a stub that records the call.
    """

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_roles: tuple[str, ...],
        case_id: UUID,
        transition_key: str,
        correlation_id: str,
        causation_id: str | None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CompleteTaskCommand:
    """Input to :func:`handle`.

    ``outcome`` is the structured closure detail (e.g.
    ``{"decision": "approved", "notes": "..."}``). The shape is
    free-form in v1 — the workflow author decides what to capture. The
    value lands on the audit row's ``payload.outcome`` and on the
    aggregate, but never on the outbox event (outbox payloads are
    id-references by policy — see ``app.domain.events``).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    task_id: UUID
    outcome: dict[str, object] | None = None
    correlation_id: str = ""
    causation_id: str | None = None
    # Optional follow-on transition key. When present the use case
    # invokes the workflow runner inside the same transaction so the
    # task completion and the case transition commit together or not
    # at all (Constitution III / P02-US1 acceptance scenario 1).
    trigger_transition_key: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteTaskResult:
    """Post-commit outcome returned to the router.

    ``task`` is the post-completion aggregate. ``transitioned`` is the
    result of the follow-on transition runner, when one was invoked —
    ``None`` when the command did not carry a transition key or when
    no transition runner was wired (e.g. unit tests).
    """

    task: Task
    transitioned: Any | None = None


async def handle(
    cmd: CompleteTaskCommand,
    *,
    task_repo: TaskRepository,
    case_lookup: _CaseLookup,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    transition_runner: _TransitionRunner | None = None,
) -> CompleteTaskResult:
    """Complete a task atomically with audit + outbox (FR-011).

    Failure behavior:

    - Auth/validation/not-found errors are raised before any DB write.
    - The task UPDATE, audit row, outbox row, and (optional) follow-on
      transition share one transaction; the caller commits once. If
      the audit or outbox write fails, the task UPDATE is rolled back.
      If the follow-on transition raises, **the task completion is
      rolled back too** — a partial commit would leave a ``completed``
      task whose guard never fired.
    - Domain-level idempotency: a second call against an
      already-``completed`` task is a no-op and writes no additional
      audit / outbox row. The aggregate stays unchanged; the result
      reports the original ``completed_at``.
    """
    # 1. Resolve the task (tenant-scoped) first so we have the case
    # id for the resource the policy governs. A missing or
    # cross-tenant task id surfaces as NotFound; the two cases are
    # indistinguishable so the API never confirms cross-tenant
    # existence (Constitution V).
    task = await task_repo.get(cmd.tenant_id, cmd.task_id)
    if task is None:
        raise NotFound(f"task {cmd.task_id} not found")

    # 1a. The task references a case — resolve it so the policy can
    # check tenant-scoped permissions against the case resource.
    case = await case_lookup.get(cmd.tenant_id, task.case_id)
    if case is None:
        raise NotFound(f"task {cmd.task_id} not found")

    # 2. Authorize. The policy raises ``Forbidden`` on deny; the HTTP
    # layer maps it to 403.
    await authorize(
        Action.TASK_COMPLETE,
        _actor_proxy(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=case.id),
    )

    # 3. Complete the task. The aggregate is idempotent — a second
    # call against an already-``completed`` task returns the same
    # shape and ``version_token``.
    now = clock.now()
    new_task = task.complete(
        actor_id=cmd.actor_id,
        outcome=cmd.outcome,
        at=now,
    )
    idempotent_replay = new_task.version_token == task.version_token

    # 4. Persist (task + audit + outbox) only when the completion is a
    # real state change. A no-op replay is reported back without
    # touching the audit chain so a duplicate completion does not
    # leave a duplicate ``TaskCompleted`` row behind.
    if not idempotent_replay:
        await task_repo.update(new_task)

        correlation_id = cmd.correlation_id or _new_correlation_id()

        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=TASK_COMPLETED,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type=SUBJECT_TYPE_TASK,
            subject_id=new_task.id,
            case_id=new_task.case_id,
            correlation_id=correlation_id,
            causation_id=cmd.causation_id,
            payload={
                "task_type": new_task.type,
                "task_title": new_task.title,
                "case_id": str(new_task.case_id),
                "outcome": dict(new_task.outcome) if new_task.outcome else {},
                "version_token": new_task.version_token,
            },
            occurred_at=now,
        )

        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=TASK_COMPLETED,
            event_id=uuid4(),
            payload={
                "task_id": str(new_task.id),
                "case_id": str(new_task.case_id),
                "tenant_id": str(cmd.tenant_id),
                "actor_id": str(cmd.actor_id),
                "outcome": dict(new_task.outcome) if new_task.outcome else {},
                "occurred_at": now.isoformat(),
            },
        )

        # 5. Optional follow-on transition. Runs only on a real
        # completion (never on an idempotent replay) and only when the
        # command carried a key. Errors propagate up so the outer
        # transaction rolls back.
        transitioned: Any | None = None
        if cmd.trigger_transition_key and transition_runner is not None:
            transitioned = await transition_runner(
                tenant_id=cmd.tenant_id,
                actor_id=cmd.actor_id,
                actor_roles=cmd.actor_roles,
                case_id=new_task.case_id,
                transition_key=cmd.trigger_transition_key,
                correlation_id=correlation_id,
                causation_id=cmd.causation_id,
            )
        return CompleteTaskResult(task=new_task, transitioned=transitioned)

    return CompleteTaskResult(task=new_task, transitioned=None)


# ----- helpers --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _actor_proxy(cmd: CompleteTaskCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""
    return _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "CompleteTaskCommand",
    "CompleteTaskResult",
    "handle",
]
