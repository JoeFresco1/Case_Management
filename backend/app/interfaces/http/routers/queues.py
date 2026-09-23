"""Queues router: admin CRUD + worker claim / release / bulk / delegate.

# INTEGRATION_SEAM: HTTP entry

The router exposes two surfaces:

- ``/api/v1/admin/queues`` — process-admin CRUD on the queue
  catalog (define / list / reconfigure / retire). Requires
  ``ADMIN_TENANT_CONFIG`` (P11-US4).
- ``/api/v1/queues/{key}/claim``, ``.../bulk-handle``,
  ``/api/v1/tasks/{id}/release``, ``/api/v1/tasks/delegate`` — the
  worker-facing claim / release / bulk / delegate flow. Requires
  ``TASK_READ`` on the queue resource.

The router delegates the application logic to
:mod:`app.application.queues.manage_queues` so the wire layer
only translates between Pydantic and the application layer. Ports
are resolved per-request via the ``QueueHttpDependencyProvider``
(SQL adapter in
``app.infrastructure.persistence.queue_http_dependencies``) so the
queue change, the audit row, and the outbox row commit in one
transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.queues import manage_queues
from app.application.queues.http_dependencies import QueueHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.queue import AssignmentPolicy, Queue, task_aging_context, visible_queues_for
from app.interfaces.http.dependencies import get_queue_dependencies
from app.interfaces.http.errors import ErrorResponse
from app.interfaces.http.schemas.queues import (
    BulkHandleRequest,
    BulkHandleResponse,
    ClaimNextResponse,
    DefineQueueRequest,
    DelegateRequest,
    DelegateResponse,
    QueueSummary,
    QueueTaskAging,
    QueueTaskView,
    ReconfigureQueueRequest,
    ReleaseTaskRequest,
    ReleaseTaskResponse,
)

router = APIRouter(tags=["queues"])


def _to_summary(queue: Queue) -> QueueSummary:
    return QueueSummary(
        id=queue.id,
        tenant_id=queue.tenant_id,
        key=queue.key,
        name=queue.name,
        description=queue.description,
        assignment_policy=queue.assignment_policy.value,  # type: ignore[return-value]
        capacity=queue.capacity,
        member_user_ids=list(queue.member_user_ids),
        rr_cursor=queue.rr_cursor,
        created_at=queue.created_at,  # type: ignore[arg-type]
        updated_at=queue.updated_at,  # type: ignore[arg-type]
    )


def _assignment_policy(value: str) -> AssignmentPolicy:
    return AssignmentPolicy(value)


def _as_aware(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to tz-aware UTC (FM-016 aging readout).

    Why: sqlite round-trips ``DateTime(timezone=True)`` as naive while
    postgres preserves the offset. The domain ``task_aging_context``
    subtracts ``now`` (always aware) from these fields, so a naive
    value raises ``TypeError`` on sqlite only — a backend difference
    the wire layer must absorb, never the domain (AGENTS.md R1:
    portable types, portable behavior).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# /api/v1/admin/queues — process-admin CRUD (P02-US3 / FM-016)
# ---------------------------------------------------------------------------


@router.post(
    "/admin/queues",
    response_model=QueueSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Define a new queue",
    responses={
        status.HTTP_409_CONFLICT: {"model": ErrorResponse, "description": "Duplicate queue key"},
        422: {
            "model": ErrorResponse,
            "description": "Validation failed",
        },
    },
)
async def define_queue(
    body: DefineQueueRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> QueueSummary:
    """Create a new queue (P02-US3 / FM-016)."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    queue = await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            key=body.key,
            name=body.name,
            description=body.description,
            assignment_policy=_assignment_policy(body.assignment_policy),
            capacity=body.capacity,
            member_user_ids=tuple(body.member_user_ids),
        ),
        queue_repo=repositories.queue,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return _to_summary(queue)


@router.get(
    "/admin/queues",
    response_model=list[QueueSummary],
    summary="List queues for the actor's tenant",
)
async def list_queues(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> list[QueueSummary]:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    queues = await repositories.queue.list_queues(user.tenant_id)
    return [_to_summary(q) for q in queues]


@router.put(
    "/admin/queues/{queue_id}",
    response_model=QueueSummary,
    summary="Reconfigure a queue",
)
async def reconfigure_queue(
    queue_id: UUID,
    body: ReconfigureQueueRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> QueueSummary:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue", id=queue_id),
        role_repository=repositories.role,
    )
    queue = await manage_queues.reconfigure_queue(
        manage_queues.ReconfigureQueueCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            queue_id=queue_id,
            name=body.name,
            description=body.description,
            assignment_policy=(
                _assignment_policy(body.assignment_policy)
                if body.assignment_policy is not None
                else None
            ),
            capacity=body.capacity,
            member_user_ids=(
                tuple(body.member_user_ids) if body.member_user_ids is not None else None
            ),
        ),
        queue_repo=repositories.queue,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return _to_summary(queue)


@router.delete(
    "/admin/queues/{queue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retire a queue",
)
async def retire_queue(
    queue_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> None:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue", id=queue_id),
        role_repository=repositories.role,
    )
    await manage_queues.retire_queue(
        manage_queues.RetireQueueCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            queue_id=queue_id,
        ),
        queue_repo=repositories.queue,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()


# ---------------------------------------------------------------------------
# /api/v1/queues — worker listing (P02-US3 / FM-016)
# ---------------------------------------------------------------------------


@router.get(
    "/queues",
    response_model=list[QueueSummary],
    summary="List queues visible to the current worker",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Actor lacks task.read",
        },
    },
)
async def list_visible_queues(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> list[QueueSummary]:
    """List the queues the caller may work (FM-016 worker distribution).

    ``MEMBERS``-scoped queues are filtered to ``member_user_ids``;
    tenant admins (``process_admin``) always see every queue. Preserves
    catalog order (``key`` ascending from the repository).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    queues = await repositories.queue.list_queues(user.tenant_id)
    is_admin = "process_admin" in user.roles
    visible = visible_queues_for(queues, user.user_id, is_admin=is_admin)
    return [_to_summary(q) for q in visible]


@router.get(
    "/queues/{queue_key}/tasks",
    response_model=list[QueueTaskView],
    summary="List open tasks on a queue with aging context",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Actor lacks task.read",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "Queue not found in tenant",
        },
    },
)
async def list_queue_tasks(
    queue_key: str = Path(min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> list[QueueTaskView]:
    """List open tasks on ``queue_key`` with per-task aging context.

    The aging readout (``age_seconds``, ``is_overdue``,
    ``overdue_by_seconds`` / ``due_in_seconds``) is computed with the
    request clock so the worker UI and the tests share one ``now``.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    queue = await repositories.queue.get_by_key(user.tenant_id, queue_key)
    if queue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code="not_found",
                message=f"queue {queue_key!r} not found",
                details={"queue_key": queue_key},
                correlation_id="",
            ).model_dump(exclude_none=True),
        )
    now = clock.now()
    tasks = await repositories.task.list_open_for_queue(user.tenant_id, queue_key, limit=100)
    views: list[QueueTaskView] = []
    for task in tasks:
        # why SimpleNamespace: the stored row may carry naive
        # timestamps on sqlite (see ``_as_aware``); the adapter keeps
        # the normalization at the wire boundary so the pure domain
        # helper never sees a backend-specific shape.
        aged = SimpleNamespace(
            due_at=_as_aware(getattr(task, "due_at", None)),
            created_at=_as_aware(getattr(task, "created_at", None)),
        )
        aging = task_aging_context(aged, now=_as_aware(now) or now)
        views.append(
            QueueTaskView(
                task_id=task.id,
                case_id=task.case_id,
                type=getattr(task, "type", ""),
                title=getattr(task, "title", ""),
                status=str(getattr(task, "status", "open")),
                due_at=getattr(task, "due_at", None),
                created_at=getattr(task, "created_at", None),
                aging=QueueTaskAging(
                    age_seconds=aging.age_seconds,
                    is_overdue=aging.is_overdue,
                    overdue_by_seconds=aging.overdue_by_seconds,
                    due_in_seconds=aging.due_in_seconds,
                ),
            )
        )
    return views


# ---------------------------------------------------------------------------
# /api/v1/queues/{key}/... + /api/v1/tasks/... — worker flow (P02-US3/US9)
# ---------------------------------------------------------------------------


@router.post(
    "/queues/{queue_key}/claim",
    response_model=ClaimNextResponse,
    status_code=status.HTTP_200_OK,
    summary="Claim the next task from a queue",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "Queue not found or no open work",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "Claim conflict (stale token)",
        },
    },
)
async def claim_next(
    queue_key: str = Path(min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> ClaimNextResponse:
    """Claim the next open task from ``queue_key`` for the actor.

    Returns 404 when the queue has no open work (the worker is told
    "nothing to do", not handed a synthetic task).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            queue_key=queue_key,
        ),
        queue_repo=repositories.queue,
        task_repo=repositories.task,
        case_lookup=repositories.case,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code="no_work_available",
                message=f"no open work on queue {queue_key!r}",
                details={"queue_key": queue_key},
                correlation_id=correlation.correlation_id,
            ).model_dump(exclude_none=True),
        )
    await db.commit()
    return ClaimNextResponse(
        task_id=result.task.id,
        queue_key=queue_key,
        claimed_at=clock.now(),
    )


@router.post(
    "/tasks/{task_id}/release",
    response_model=ReleaseTaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Release a claimed task back to its queue",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Task not found"},
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "Task not owned by actor / no return queue",
        },
    },
)
async def release_task(
    task_id: UUID,
    body: ReleaseTaskRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> ReleaseTaskResponse:
    """Release ``task_id`` back to its prior queue (claim is reversible)."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="task", id=task_id),
        role_repository=repositories.role,
    )
    task = await manage_queues.release_task(
        manage_queues.ReleaseTaskCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            task_id=task_id,
            reason=body.reason,
            queue_key=body.queue_key,
        ),
        task_repo=repositories.task,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return ReleaseTaskResponse(task_id=task.id, status="released")


@router.post(
    "/queues/{queue_key}/bulk-handle",
    response_model=BulkHandleResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk claim or release a batch of tasks",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Queue not found"},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse, "description": "Bulk conflict"},
    },
)
async def bulk_handle(
    body: BulkHandleRequest,
    queue_key: str = Path(min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> BulkHandleResponse:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="queue"),
        role_repository=repositories.role,
    )
    result = await manage_queues.bulk_handle(
        manage_queues.BulkHandleCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            action=body.action,
            task_ids=tuple(body.task_ids),
            queue_key=queue_key,
        ),
        queue_repo=repositories.queue,
        task_repo=repositories.task,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return BulkHandleResponse(
        handled=[t.id for t in result.handled],
        skipped=list(result.skipped),
    )


@router.post(
    "/tasks/delegate",
    response_model=DelegateResponse,
    status_code=status.HTTP_200_OK,
    summary="Delegate open tasks to another user for a bounded window (P02-US9)",
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "Past window, self-delegation, or stale token",
        },
        422: {"model": ErrorResponse, "description": "Validation failed"},
    },
)
async def delegate_tasks(
    body: DelegateRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: QueueHttpDependencyProvider = Depends(get_queue_dependencies),
) -> DelegateResponse:
    """Delegate the actor's open tasks to ``delegate_user_id`` until ``until``."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.TASK_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="user", id=body.delegate_user_id),
        role_repository=repositories.role,
    )
    delegated = await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            delegate_user_id=body.delegate_user_id,
            task_ids=tuple(body.task_ids),
            until=body.until,
        ),
        task_repo=repositories.task,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return DelegateResponse(
        delegated=[t.id for t in delegated],
        delegate_user_id=body.delegate_user_id,
        until=body.until,
    )


__all__ = ["router"]
