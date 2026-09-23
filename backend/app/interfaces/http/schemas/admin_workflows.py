"""Pydantic request/response schemas for the admin workflows API (FM-013 / FR-015).

Mirrors the canonical :class:`app.domain.workflow.WorkflowVersion` and
:class:`app.domain.workflow.WorkflowDefinition` entities. The HTTP
layer is intentionally a **projection**: it does not invent fields the
domain does not already own. This keeps the wire format compatible with
the long-lived declarative contract from AGENTS.md §16.2 (workflow
``definition_json`` shape).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

WorkflowVersionStatus = Literal["draft", "published", "retired"]


class StateDefinitionRequest(BaseModel):
    """Wire shape of a single state in a workflow version."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    is_initial: bool = False
    is_terminal: bool = False


class TransitionDefinitionRequest(BaseModel):
    """Wire shape of a single transition in a workflow version.

    ``human_driven`` is the cycle-breaking flag the validator uses to
    distinguish a breakable reviewer loop (``intake -> in_review ->
    needs_info -> in_review``) from a runaway automatic loop (plan §5).
    """

    key: str = Field(min_length=1, max_length=64)
    from_state: str = Field(min_length=1, max_length=64)
    to_state: str = Field(min_length=1, max_length=64)
    human_driven: bool = False
    guard: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDefinitionRequest(BaseModel):
    """The full ``definition_json`` body for a draft workflow version.

    Validated by ``app.application.configuration.validate_definition``;
    on failure the request returns 422 ``validation_failed`` and no row
    is persisted.
    """

    states: list[StateDefinitionRequest] = Field(default_factory=list)
    transitions: list[TransitionDefinitionRequest] = Field(default_factory=list)


class DraftWorkflowVersionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/workflows/{key}/versions``.

    The router translates the wire states/transitions into the domain
    :class:`WorkflowState` and :class:`WorkflowTransition` dataclasses
    before handing them to the use case; the validator runs after that
    translation.
    """

    definition: WorkflowDefinitionRequest


class CreateWorkflowRequest(BaseModel):
    """Body for creating a workflow definition header."""

    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class WorkflowDefinitionResponse(BaseModel):
    """Created workflow definition header."""

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str
    current_version_id: UUID | None


class DraftWorkflowResultResponse(BaseModel):
    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int
    status: WorkflowVersionStatus


class PublishWorkflowResultResponse(BaseModel):
    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int
    status: WorkflowVersionStatus


class WorkflowStateResponse(BaseModel):
    """Read-only state projection for the admin workflow viewer."""

    key: str
    label: str
    is_initial: bool
    is_terminal: bool


class WorkflowTransitionResponse(BaseModel):
    """Read-only transition projection for the admin workflow viewer."""

    key: str
    from_state: str
    to_state: str
    human_driven: bool
    guard: dict[str, Any] | None
    actions: list[dict[str, Any]]


class WorkflowVersionResponse(BaseModel):
    """Full immutable workflow version projection."""

    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int
    status: WorkflowVersionStatus
    states: list[WorkflowStateResponse]
    transitions: list[WorkflowTransitionResponse]


class WorkflowSummaryResponse(BaseModel):
    """Workflow header and its effective version pointer."""

    id: UUID
    key: str
    name: str
    description: str
    current_version_id: UUID | None
    current_version_number: int | None


class WorkflowDiffResponse(BaseModel):
    """Stable, read-only summary of changes from the previous version."""

    workflow_definition_id: UUID
    version_number: int
    previous_version_number: int | None
    added_states: list[str]
    removed_states: list[str]
    added_transitions: list[str]
    removed_transitions: list[str]


class WorkflowValidationResponse(BaseModel):
    """Response from ``POST /api/v1/admin/workflows/{key}/versions:validate``.

    Returns the parsed ``definition_json`` snapshot so the admin UI can
    render what would be published without persisting a row. The
    response is intentionally small: it carries the id the admin UI
    needs (workflow + queue set) and confirms the validator passed.
    """

    workflow_definition_id: UUID
    states: list[str]
    transitions: list[str]


# ---------------------------------------------------------------------------
# Simulation (FM-015 / P02-US5)
# ---------------------------------------------------------------------------


SimulationOutcome = Literal["completed", "blocked", "exception"]
SimulationBlockerKind = Literal["no_matching_transition", "guard_failed", "invalid_request"]


class SimulationActionRequest(BaseModel):
    """One actor decision in the simulation script.

    The actor supplies the ``transition_key`` (the transition they
    would fire from the current state). ``payload`` is forwarded to
    the runtime engine; the simulator does not interpret it, but
    accepts and echoes it for round-trip parity with the live endpoint.
    """

    transition_key: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class SimulateWorkflowVersionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/workflows/{id}/versions/{n}/simulate``.

    ``synthetic_case`` is the case data the guards will be evaluated
    against. It is **never** persisted; the endpoint is a dry-run. The
    optional ``actions`` list drives deterministic progression: when
    omitted, the simulator auto-progresses through the first available
    transition at every state.
    """

    synthetic_case: dict[str, Any] = Field(default_factory=dict)
    actions: list[SimulationActionRequest] = Field(default_factory=list)


class SimulatedGuardResultResponse(BaseModel):
    rule: dict[str, Any]
    inputs: dict[str, Any]
    result: bool


class SimulatedStepResponse(BaseModel):
    transition: str
    from_state: str
    to_state: str
    guards: list[SimulatedGuardResultResponse]
    actions_fired: list[dict[str, Any]]


class SimulationBlockerResponse(BaseModel):
    kind: SimulationBlockerKind
    message: str
    transition: str | None = None
    state: str | None = None
    failed_guard: SimulatedGuardResultResponse | None = None


class SimulateWorkflowVersionResponse(BaseModel):
    """Response for the simulation endpoint.

    ``outcome`` is ``completed`` / ``blocked`` / ``exception``. ``steps``
    is the ordered list of every transition the simulator fired; each
    step carries the evaluated guards so the admin UI can render a
    per-clause breakdown. ``blockers`` is non-empty when ``outcome``
    is ``blocked`` or ``exception``.
    """

    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int
    status: WorkflowVersionStatus
    initial_state: str
    final_state: str
    outcome: SimulationOutcome
    steps: list[SimulatedStepResponse] = Field(default_factory=list)
    blockers: list[SimulationBlockerResponse] = Field(default_factory=list)


__all__ = [
    "DraftWorkflowResultResponse",
    "DraftWorkflowVersionRequest",
    "CreateWorkflowRequest",
    "WorkflowDefinitionResponse",
    "PublishWorkflowResultResponse",
    "WorkflowDiffResponse",
    "WorkflowStateResponse",
    "WorkflowSummaryResponse",
    "WorkflowTransitionResponse",
    "WorkflowVersionResponse",
    "SimulationActionRequest",
    "SimulationBlockerKind",
    "SimulationBlockerResponse",
    "SimulationOutcome",
    "SimulateWorkflowVersionRequest",
    "SimulateWorkflowVersionResponse",
    "SimulatedGuardResultResponse",
    "SimulatedStepResponse",
    "StateDefinitionRequest",
    "TransitionDefinitionRequest",
    "WorkflowDefinitionRequest",
    "WorkflowValidationResponse",
    "WorkflowVersionStatus",
]
