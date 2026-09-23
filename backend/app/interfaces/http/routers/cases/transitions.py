"""Workflow transition routes on a case: GET/POST /api/v1/cases/{id}/transitions.

# INTEGRATION_SEAM: HTTP entry

P02-US1 / FR-009 / T076: the canonical entry point for the workflow
engine. ``GET`` preflights the currently available transitions with
deterministic guard results (read-only discovery); ``POST`` executes
one transition, evaluates guards, runs engine actions, and returns the
post-transition state plus the follow-on tasks.

Delegates to ``app.application.workflow.preflight_transitions`` and
``app.application.workflow.execute_transition``; authorization
(``Action.CASE_TRANSITION``) is evaluated inside the use cases. The
HTTP ``IdempotencyMiddleware`` is the canonical replay mechanism for
transport-level retries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.workflow import execute_transition as transition_uc  # noqa: F401
from app.application.workflow import preflight_transitions as preflight_uc
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.cases import (
    ExecuteTransitionRequest,
    ExecuteTransitionResponse,
    TaskSummary,
    TransitionPreflight,
    TransitionPreflightResponse,
)

from .common import router

# ---------------------------------------------------------------------------
# GET/POST /api/v1/cases/{id}/transitions  (P02-US1 / FR-009 / T076)
# ---------------------------------------------------------------------------


@router.get(
    "/{case_id}/transitions",
    response_model=TransitionPreflightResponse,
    summary="List current workflow transitions and deterministic guard results",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.transition"},
        status.HTTP_404_NOT_FOUND: {"description": "Case or workflow instance not found"},
    },
)
async def list_transitions(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> TransitionPreflightResponse:
    """Discover transition options; this operation never changes case state."""
    repositories = dependencies.for_session(db)
    result = await preflight_uc.handle(
        preflight_uc.PreflightTransitionsCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
        ),
        case_repo=repositories.case,
        instance_repo=repositories.workflow_instance,
        workflow_version_repo=repositories.workflow_version,
        task_repo=repositories.task,
        document_repo=repositories.document,
    )
    return TransitionPreflightResponse(
        case_id=result.case_id,
        workflow_instance_id=result.workflow_instance_id,
        workflow_version_id=result.workflow_version_id,
        current_state=result.current_state,
        transitions=[
            TransitionPreflight(
                key=verdict.key,
                from_state=verdict.from_state,
                to_state=verdict.to_state,
                allowed=verdict.allowed,
                reason=verdict.reason,
                details=verdict.details,
                approval_required=verdict.approval_required,
                approval_eligible=verdict.approval_eligible,
                approval_reason=verdict.approval_reason,
            )
            for verdict in result.transitions
        ],
    )


@router.post(
    "/{case_id}/transitions",
    summary="Execute a workflow transition on a case",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks case.transition",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Case or its workflow instance not found",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Transition not allowed from current state",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transition guard rejected (FR-010)",
        },
    },
)
async def execute_transition(
    case_id: UUID,
    body: ExecuteTransitionRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> ExecuteTransitionResponse:
    """Execute a workflow transition with deterministic guard evaluation.

    The endpoint is the canonical entry point for the workflow engine
    (P02-US1 / FR-009). The request body carries the transition key
    and an optional payload; the response reports the post-transition
    state and the follow-on tasks the engine created.

    The HTTP ``IdempotencyMiddleware`` is the canonical replay
    mechanism for transport-level retries. The use case itself does
    not need to be idempotent at the domain layer — the workflow
    instance pointer moves every call — so callers that want
    at-most-once semantics should always carry an ``Idempotency-Key``.
    """
    repositories = dependencies.for_session(db)
    result = await transition_uc.handle(
        transition_uc.ExecuteTransitionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
            transition_key=body.transition_key,
            payload=body.payload,
            correlation_id=correlation_id,
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
    await db.commit()
    return ExecuteTransitionResponse(
        case_id=result.case.id,
        from_state=result.from_state,
        to_state=result.to_state,
        transition_key=result.transition_key,
        created_tasks=[
            TaskSummary(
                id=task.id,
                type=task.type,
                title=task.title,
                assignee_queue_id=task.assignee_queue_id,
                assignee_user_id=task.assignee_user_id,
            )
            for task in result.tasks
        ],
    )
