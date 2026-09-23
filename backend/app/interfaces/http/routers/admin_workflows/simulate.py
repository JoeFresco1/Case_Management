"""Workflow simulation route: deterministic dry-run of a version (FM-015 / P02-US5).

# Why this module (R2 structural split)

Route group extracted from the former single-file
``admin_workflows.py`` (722 lines). This module owns the simulation
surface:

- ``POST /api/v1/admin/workflows/{id}/versions/{n}/simulate`` — dry-run
  a draft or published ``WorkflowVersion`` against a synthetic case.
  The endpoint never mutates the version, never persists the synthetic
  case, and never writes audit events. It is the companion to the
  ``execute_transition`` runtime engine and answers "what would happen
  if this case took these transitions?"

Plus the domain→wire projections for the deterministic trace
(:func:`_guard_to_response`, :func:`_step_to_response`,
:func:`_blocker_to_response`) that only this route consumes.

# Authorization

Requires ``Action.WORKFLOW_PUBLISH`` (granted to the ``process_admin``
static role); fail-closed → ``403 Forbidden``. The endpoint is
**read-only**: it commits nothing. Published versions are readable for
simulation just like draft versions; undeclared transition keys
surface as ``no_matching_transition`` blockers rather than mutations.

# INTEGRATION_SEAM: HTTP entry

The pure simulator is ``app.application.workflow.simulate_workflow.simulate``;
persistence reads reach the process through the
``AdminWorkflowsHttpDependencyProvider`` session bundle. No LLM or
external call participates — the trace is deterministic
(Constitution XIV).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.workflow.http_dependencies import AdminWorkflowsHttpDependencyProvider
from app.application.workflow.simulate_workflow import (
    SimulatedGuardResult as SimulatedGuardResultDomain,
)
from app.application.workflow.simulate_workflow import (
    SimulatedStep as SimulatedStepDomain,
)
from app.application.workflow.simulate_workflow import (
    SimulationBlocker as SimulationBlockerDomain,
)
from app.application.workflow.simulate_workflow import (
    SimulationResult,
)
from app.application.workflow.simulate_workflow import (
    simulate as simulate_workflow_use_case,
)
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_admin_workflows_dependencies
from app.interfaces.http.schemas.admin_workflows import (
    SimulatedGuardResultResponse,
    SimulatedStepResponse,
    SimulateWorkflowVersionRequest,
    SimulateWorkflowVersionResponse,
    SimulationBlockerResponse,
)

from .common import router


def _guard_to_response(
    guard: SimulatedGuardResultDomain,
) -> SimulatedGuardResultResponse:
    """Project one simulated guard evaluation onto the wire schema."""
    return SimulatedGuardResultResponse(rule=guard.rule, inputs=guard.inputs, result=guard.result)


def _step_to_response(step: SimulatedStepDomain) -> SimulatedStepResponse:
    """Project one simulation step (transition + guards) onto the wire schema."""
    return SimulatedStepResponse(
        transition=step.transition,
        from_state=step.from_state,
        to_state=step.to_state,
        guards=[_guard_to_response(g) for g in step.guards],
        actions_fired=list(step.actions_fired),
    )


def _blocker_to_response(
    blocker: SimulationBlockerDomain,
) -> SimulationBlockerResponse:
    """Project one simulation blocker (why the dry-run halted) onto the wire schema."""
    return SimulationBlockerResponse(
        kind=blocker.kind,
        message=blocker.message,
        transition=blocker.transition,
        state=blocker.state,
        failed_guard=(
            _guard_to_response(blocker.failed_guard) if blocker.failed_guard is not None else None
        ),
    )


@router.post(
    "/{workflow_id}/versions/{version_number}/simulate",
    response_model=SimulateWorkflowVersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Dry-run a WorkflowVersion against a synthetic case (FM-015 / P02-US5)",
)
async def simulate_workflow_version(
    workflow_id: UUID = ...,
    version_number: int = Path(ge=1),
    body: SimulateWorkflowVersionRequest = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminWorkflowsHttpDependencyProvider = Depends(get_admin_workflows_dependencies),
) -> SimulateWorkflowVersionResponse:
    """Simulate ``WorkflowVersion`` ``version_number`` of workflow ``workflow_id``.

    The endpoint is **read-only** -- it never mutates the version, never
    persists the synthetic case, and never writes audit or outbox
    rows. It is the dry-run companion to the runtime
    ``execute_transition`` engine.

    Authorization mirrors the rest of the admin workflow surface:
    ``Action.WORKFLOW_PUBLISH`` (granted to ``process_admin``). The
    endpoint refuses simulation of a **published** version's mutation
    surface -- but the simulator itself never mutates anything; the
    guard exists so a misuse (e.g. an attacker crafting a body that
    carries publish-shaped actions) cannot bypass the workflow
    lifecycle gates. Published versions are therefore readable for
    simulation just like draft versions; the rejection criterion is
    that the simulated ``actions`` list cannot include keys that are
    not declared on the version (the simulator surfaces those as
    ``no_matching_transition`` blockers).

    The response carries the initial state, the final state, the
    ordered step trace, and the blockers (if any). The trace is
    deterministic -- identical inputs produce identical output, which
    the tests assert directly.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.WORKFLOW_PUBLISH,
        user,
        Resource(tenant_id=user.tenant_id, kind="workflow"),
        role_repository=repositories.role,
    )

    workflow = await repositories.workflow.get(user.tenant_id, workflow_id)
    if workflow is None:
        raise NotFound(f"workflow definition {workflow_id} not found")

    version = await repositories.workflow_version.get_by_version_number(
        user.tenant_id, workflow.id, version_number
    )
    if version is None:
        raise NotFound(f"workflow version {version_number} of definition {workflow.id} not found")

    actions_payload: list[dict[str, Any]] = [
        {"transition_key": a.transition_key, "payload": dict(a.payload)} for a in body.actions
    ]
    result: SimulationResult = simulate_workflow_use_case(
        version, body.synthetic_case, actions=actions_payload or None
    )

    return SimulateWorkflowVersionResponse(
        workflow_definition_id=workflow.id,
        workflow_version_id=version.id,
        version_number=version.version_number,
        status=version.status.value,
        initial_state=result.initial_state,
        final_state=result.final_state,
        outcome=result.outcome,
        steps=[_step_to_response(s) for s in result.steps],
        blockers=[_blocker_to_response(b) for b in result.blockers],
    )
