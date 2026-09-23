"""Workflow draft and candidate-validation routes (keyed by workflow key).

# Why this module (R2 structural split)

Route group extracted from the former single-file
``admin_workflows.py`` (722 lines). This module owns the *authoring*
surface addressed by the human-stable workflow ``key``:

- ``POST /api/v1/admin/workflows/{key}/versions`` — draft a new
  ``WorkflowVersion`` of an existing ``WorkflowDefinition`` (FM-013 /
  FR-015, P02-US2). The use case runs the validator at draft time so a
  broken definition is never persisted.
- ``POST /api/v1/admin/workflows/{key}/versions:validate`` — run the
  validator against the supplied ``definition`` without persisting
  anything. Returns the parsed ``state_keys`` / ``transition_keys``
  so the admin UI can render what would be published.

The wire→domain translators :func:`_to_domain_states` and
:func:`_to_domain_transitions` live here because both endpoints share
them: the domain dataclasses enforce key/label invariants
(``InvariantViolation`` → ``422``) while the validator runs the deeper
shape checks (``op``, ``clauses`` nesting depth, etc.).

# Authorization

Both endpoints require ``Action.WORKFLOW_PUBLISH`` (granted to the
``process_admin`` static role); fail-closed → ``403 Forbidden``.
Draft commits once after the use case returns so the new version row,
the audit event, and any outbox row land atomically (Constitution IV /
plan §4.R12). The validate endpoint persists nothing.

# INTEGRATION_SEAM: HTTP entry

Business decisions delegate to ``app.application.workflow.draft_workflow``
and the pure ``app.application.configuration.validate_definition``
validator; persistence reaches the process through the
``AdminWorkflowsHttpDependencyProvider`` session bundle.
"""

from __future__ import annotations

from fastapi import Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.configuration.validate_definition import (
    validate_definition as validate_workflow_definition,
)
from app.application.workflow import draft_workflow
from app.application.workflow.http_dependencies import AdminWorkflowsHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import NotFound
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)
from app.interfaces.http.dependencies import get_admin_workflows_dependencies
from app.interfaces.http.schemas.admin_workflows import (
    DraftWorkflowResultResponse,
    DraftWorkflowVersionRequest,
    WorkflowValidationResponse,
)

from .common import router


def _to_domain_states(body: DraftWorkflowVersionRequest) -> tuple[WorkflowState, ...]:
    """Translate wire state requests into the domain :class:`WorkflowState`.

    The domain ``WorkflowState.__post_init__`` enforces key/label
    invariants; any defect here raises ``InvariantViolation`` which the
    error handler maps to ``422 validation_failed``.
    """
    return tuple(
        WorkflowState(
            key=s.key,
            label=s.label,
            is_initial=s.is_initial,
            is_terminal=s.is_terminal,
        )
        for s in body.definition.states
    )


def _to_domain_transitions(
    body: DraftWorkflowVersionRequest,
) -> tuple[WorkflowTransition, ...]:
    """Translate wire transition requests into the domain ``WorkflowTransition``.

    ``actions`` is converted to a tuple so the domain dataclass'
    ``frozen=True`` contract holds. The validator runs the deeper
    shape checks (``op``, ``clauses`` nesting depth, etc.).
    """
    return tuple(
        WorkflowTransition(
            key=t.key,
            from_state=t.from_state,
            to_state=t.to_state,
            human_driven=t.human_driven,
            guard=t.guard,
            actions=tuple(t.actions),
        )
        for t in body.definition.transitions
    )


@router.post(
    "/{workflow_key}/versions",
    response_model=DraftWorkflowResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a new WorkflowVersion (FM-013 / FR-015)",
)
async def draft_workflow_version(
    workflow_key: str = Path(min_length=1, max_length=64),
    body: DraftWorkflowVersionRequest = ...,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> DraftWorkflowResultResponse:
    """Draft a new ``WorkflowVersion`` for an existing ``WorkflowDefinition``.

    The validator runs at draft time so a malformed definition is
    rejected before any row is persisted. Idempotency: re-drafting the
    same body creates a new version row (version_number monotonically
    increases). Publishing the draft flips status atomically; the
    draft itself is never mutated after creation.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )

    result = await draft_workflow.draft_handle(
        draft_workflow.DraftWorkflowVersionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            workflow_key=workflow_key,
            states=_to_domain_states(body),
            transitions=_to_domain_transitions(body),
            correlation_id=correlation.correlation_id,
        ),
        workflow_repo=repositories.workflow,
        workflow_version_repo=repositories.workflow_version,
        queue_repo=repositories.queue,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return DraftWorkflowResultResponse(
        workflow_definition_id=result.workflow_definition_id,
        workflow_version_id=result.workflow_version_id,
        version_number=result.version_number,
        status="draft",
    )


@router.post(
    "/{workflow_key}/versions:validate",
    response_model=WorkflowValidationResponse,
    status_code=status.HTTP_200_OK,
    summary="Validate a workflow definition without persisting",
)
async def validate_workflow_definition_endpoint(
    workflow_key: str = Path(min_length=1, max_length=64),
    body: DraftWorkflowVersionRequest = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> WorkflowValidationResponse:
    """Run the workflow validator over a candidate ``definition``.

    The validator is a pure function over the domain entity — no
    persistence side effects. The endpoint exists so the admin UI can
    preview a definition before committing a draft. The response
    carries the parsed ``state_keys`` / ``transition_keys`` so the UI
    can render the normalized form.

    Authorization is the same ``Action.WORKFLOW_PUBLISH`` check the
    draft endpoint enforces; we fail closed (R3 / R12).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )

    workflow = await repositories.workflow.get_by_key(user.tenant_id, workflow_key)
    if workflow is None:
        raise NotFound(f"workflow definition {workflow_key!r} not found")

    states = _to_domain_states(body)
    transitions = _to_domain_transitions(body)
    queue_keys = await repositories.queue.list_keys(user.tenant_id)

    # Build an ephemeral WorkflowVersion in DRAFT status so the
    # validator can consume its ``states`` / ``transitions`` accessors.
    # The row is **not** persisted; we throw it away after the
    # validator returns.
    candidate = WorkflowVersion.draft(
        tenant_id=user.tenant_id,
        workflow_definition_id=workflow.id,
        version_number=0,
        states=states,
        transitions=transitions,
        now=None,
    )
    validate_workflow_definition(candidate, known_queues=queue_keys)

    return WorkflowValidationResponse(
        workflow_definition_id=workflow.id,
        states=list(candidate.state_keys()),
        transitions=list(candidate.transition_keys()),
    )
