"""Shared helpers for the operational-report use cases.

Split out of the former single-file ``operational_reports.py`` during the
R2 package split: team resolution through the live ``QueueRepository``,
row projections from domain aggregates onto the wire shapes, the
page-limit guard, the auditable authorization-scope marker, and the
duck-typed actor proxy the authorization policy consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.case import Case
from app.domain.errors import ValidationFailed
from app.domain.ports import QueueRepository

from .shapes import FailedWorkRow, OpenCaseRow, OverdueTaskRow


async def _resolve_team(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    queue_repo: QueueRepository,
) -> list[str]:
    """Return the queue keys the actor is a member of.

    The membership list is read live from the queue repository so a
    revocation of queue membership takes effect on the very next
    report. A cross-tenant queue id cannot match because the
    ``list_queues`` call is tenant-scoped.

    Empty result -> the actor has no team; the caller renders an
    empty report rather than a 404 (the report is about "the team
    the actor belongs to" — an actor with no team has no rows by
    construction).
    """
    actor_ctx = _ActorProxy(
        tenant_id=tenant_id,
        user_id=actor_id,
        roles=actor_roles,
    )
    # Authorize the queue read; supervisors and reviewers both hold
    # TASK_READ / CASE_READ but the queue listing itself is gated by
    # the policy so a misconfigured role grant cannot widen the team
    # scope silently.
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=tenant_id, kind="queue", id=None),
    )

    queues = await queue_repo.list_queues(tenant_id)
    keys: list[str] = []
    for queue in queues:
        # The queue aggregate exposes the membership tuple; the
        # ``in`` check is O(n) but the membership list is bounded by
        # the team size (a handful of users in v1), so the linear
        # scan is fine. The repository is the authoritative source.
        members = getattr(queue, "member_user_ids", ()) or ()
        if actor_id in members:
            keys.append(queue.key)
    return keys


def _case_to_row(case: Case) -> OpenCaseRow:
    """Project a :class:`Case` onto the wire row."""
    return OpenCaseRow(
        case_id=case.id,
        reference=str(case.reference),
        case_type_version_id=case.case_type_version_id,
        status=case.status.value if hasattr(case.status, "value") else str(case.status),
        priority=case.priority,
        title=case.title,
        current_workflow_state=case.current_workflow_state,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _task_to_row(task: object, *, now: datetime) -> OverdueTaskRow:
    """Project a task aggregate onto the wire row.

    Accepts the broad ``object`` type so the use case does not have
    to import the ``Task`` aggregate just to read attribute names —
    the wire shape is the contract.
    """
    due_at = getattr(task, "due_at", None)
    # Why ``total_seconds``: a future Pydantic schema serializes the
    # overdue window as a plain integer; ``datetime.timedelta`` is
    # not JSON-serializable. ``int`` is the OpenAPI contract.
    overdue_seconds = int((now - due_at).total_seconds()) if due_at is not None else 0
    return OverdueTaskRow(
        task_id=task.id,  # type: ignore[attr-defined]
        case_id=task.case_id,  # type: ignore[attr-defined]
        type=getattr(task, "type", ""),
        title=getattr(task, "title", ""),
        priority=getattr(task, "priority", "normal"),
        assignee_user_id=getattr(task, "assignee_user_id", None),
        assignee_queue_id=getattr(task, "assignee_queue_id", None),
        due_at=cast(datetime, due_at),
        overdue_seconds=overdue_seconds,
    )


def _failed_to_row(item: object) -> FailedWorkRow:
    """Project a failed-work adapter row onto :class:`FailedWorkRow`.

    Accepts ``FailedWorkRow`` instances directly (test fakes) or any
    object exposing the same attribute names (the outbox SQL adapter
    view). ``last_error`` is passed through verbatim — adapters must
    already strip stacks/secrets (governed export rule).
    """
    if isinstance(item, FailedWorkRow):
        return item
    work_id = getattr(item, "work_id", getattr(item, "id", None))
    return FailedWorkRow(
        work_id=cast(UUID, work_id),
        event_type=str(getattr(item, "event_type", "unknown")),
        queue_key=getattr(item, "queue_key", getattr(item, "assignee_queue_id", None)),
        case_id=getattr(item, "case_id", None),
        attempt_count=int(getattr(item, "attempt_count", 0) or 0),
        last_error=getattr(item, "last_error", None),
        first_failed_at=cast(
            datetime, getattr(item, "first_failed_at", getattr(item, "created_at", None))
        ),
        last_attempt_at=cast(
            datetime, getattr(item, "last_attempt_at", getattr(item, "created_at", None))
        ),
        replay_eligible=bool(getattr(item, "replay_eligible", True)),
    )


def _enforce_limit(limit: int) -> None:
    """Clamp the report page size to the contract.

    Mirrors ``view_case.DEFAULT_PAGE_LIMIT`` semantics (``[1, 200]``)
    so the wire contract stays consistent across the API surface.
    The supervisor UI can request smaller pages; the server refuses
    anything outside the range.
    """
    if limit < 1 or limit > 200:
        raise ValidationFailed("limit must be in [1, 200]", field="limit")


def build_authorization_scope(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    team: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Build the auditable authorization-scope marker for a report.

    The scope answers "who saw what under which grant" without
    leaking other teams: tenant + actor + sorted role names + sorted
    team queue keys. Every snapshot and KPI embeds this so a manager
    can verify the numbers cover exactly their team and no wider
    tenant slice. Deterministic ordering keeps audit replay stable.
    """
    scope: list[str] = [f"tenant:{tenant_id}", f"actor:{actor_id}"]
    scope.extend(f"role:{role}" for role in sorted(actor_roles))
    scope.extend(f"team:{queue}" for queue in sorted(team))
    return tuple(scope)


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see view_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "_ActorProxy",
    "_case_to_row",
    "_enforce_limit",
    "_failed_to_row",
    "_resolve_team",
    "_task_to_row",
    "build_authorization_scope",
]
