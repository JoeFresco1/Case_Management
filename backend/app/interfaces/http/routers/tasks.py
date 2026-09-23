"""Tasks router (P02-US1 / FM-014 / FR-011).

# INTEGRATION_SEAM: HTTP entry

``POST /api/v1/tasks/{id}/complete`` is the canonical entry point for
P02-US1's "case worker completes a task and triggers a workflow
transition" story. It requires ``Idempotency-Key`` (FR-011 / AGENTS.md
§10) and delegates to ``app.application.tasks.complete_task.handle``,
which performs authorization, idempotent completion, and the atomic
task + audit + outbox write.

``GET /api/v1/tasks?assignee=me|queue:<key>&status=open|all`` is the
P02-US3 worker queue read shape. The list endpoint paginates via the
canonical ``Page[T]`` envelope (``app/interfaces/http/schemas/cases.py``
carries the shared ``Page[T]`` declaration today; tasks get its own
alias here for clarity).

The HTTP ``IdempotencyMiddleware`` handles replay; the handler does
not need to know about keys — the domain layer is already idempotent
(:meth:`Task.complete` is a no-op on a second call).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.tasks import complete_task as complete_task_uc
from app.application.tasks import list_tasks as list_tasks_uc
from app.application.workflow import execute_transition as transition_uc
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.task import Task
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.cases import Page
from app.interfaces.http.schemas.tasks import (
    CompleteTaskRequest,
    TaskView,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _task_view(task: Task) -> TaskView:
    """Translate a domain task to the JSON-safe HTTP representation."""
    return TaskView(
        id=task.id,
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        type=task.type,
        title=task.title,
        status=task.status.value,
        priority=task.priority,
        assignee_user_id=task.assignee_user_id,
        assignee_queue_id=task.assignee_queue_id,
        due_at=task.due_at.isoformat() if task.due_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        completed_by=task.completed_by,
        outcome=task.outcome,
        version_token=task.version_token,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/tasks/{id}/complete  (P02-US1 / FR-011)
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/complete",
    response_model=TaskView,
    status_code=status.HTTP_200_OK,
    summary="Complete a task (idempotent, FR-011)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Idempotency-Key header is required",
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks task.complete"},
        status.HTTP_404_NOT_FOUND: {
            "description": "Task or its case not found in tenant",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Task already in a terminal state or stale token",
        },
    },
)
async def complete_task(
    task_id: UUID,
    body: CompleteTaskRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),  # noqa: ARG001 - reserved
) -> TaskView:
    # FR-011: POST /api/v1/tasks/{id}/complete requires an
    # Idempotency-Key header. We reject with 400 before opening the
    # transaction so a missing key never reaches the use case.
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    repositories = dependencies.for_session(db)

    async def run_transition(
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_roles: tuple[str, ...],
        case_id: UUID,
        transition_key: str,
        correlation_id: str,
        causation_id: str | None,
    ) -> transition_uc.ExecuteTransitionResult:
        """Run the canonical workflow engine in this request transaction."""
        return await transition_uc.handle(
            transition_uc.ExecuteTransitionCommand(
                tenant_id=tenant_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                case_id=case_id,
                transition_key=transition_key,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ),
            case_repo=repositories.case,
            instance_repo=repositories.workflow_instance,
            workflow_version_repo=repositories.workflow_version,
            task_repo=repositories.task,
            audit=repositories.audit,
            outbox=repositories.outbox,
            clock=clock,
            document_repo=repositories.document,
            sla_clock_repo=repositories.sla_clock,
            case_type_version_repo=repositories.case_type_version,
            transition_log=repositories.workflow_transition_log,
        )

    result = await complete_task_uc.handle(
        complete_task_uc.CompleteTaskCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            task_id=task_id,
            outcome=body.outcome,
            trigger_transition_key=body.trigger_transition_key,
            correlation_id=correlation_id,
        ),
        task_repo=repositories.task,
        case_lookup=repositories.case,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
        transition_runner=run_transition,
    )
    await db.commit()
    return _task_view(result.task)


# ---------------------------------------------------------------------------
# GET /api/v1/tasks  (P02-US3 / FM-014)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Page[TaskView],
    summary="List tasks visible to the current actor",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks task.read"},
    },
)
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: UserContext = Depends(get_current_user),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    assignee: Annotated[  # noqa: ARG001 - reserved for T063
        str | None,
        Query(
            description=(
                "Filter by assignee: 'me' for the current actor, or 'queue:<key>' for a queue key."
            )
        ),
    ] = None,
    status_filter: Annotated[  # noqa: ARG001 - reserved for T063
        str | None,
        Query(
            alias="status",
            description="Filter by task status: 'open' or 'all' (default 'open').",
        ),
    ] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TaskView]:
    """List tasks for the current actor's queue.

    Why this is the placeholder shape: the full queue policy
    (round-robin, manual claim/release, SLA-aware aging) lands with
    P02-US3 in a later slice. The v1 router accepts ``assignee=me`` and
    ``assignee=queue:<key>`` and returns the tasks the actor owns or
    the queue holds, ordered by ``due_at ASC NULLS LAST,
    created_at ASC``. A real implementation will land alongside the
    SQL ``tasks`` adapter (T063).
    """
    if assignee is not None and assignee != "me" and not assignee.startswith("queue:"):
        raise HTTPException(status_code=422, detail="assignee must be 'me' or 'queue:<key>'")
    if status_filter not in ("open", "all"):
        raise HTTPException(status_code=422, detail="status must be 'open' or 'all'")
    repositories = dependencies.for_session(db)
    tasks = await list_tasks_uc.handle(
        list_tasks_uc.ListTasksCommand(
            user.tenant_id,
            user.user_id,
            user.roles,
            assignee,
            status_filter or "open",
            limit,
            offset,
        ),
        task_repo=repositories.task,
    )
    return Page[TaskView](items=[_task_view(task) for task in tasks], next_cursor=None, limit=limit)


__all__ = ["router"]
