"""Pydantic request/response schemas for the forms API (P07 / FM-036)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class FormFieldSpec(BaseModel):
    """A single field on a form."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    required: bool = False
    placeholder: str | None = None
    help_text: str | None = None


class FormSectionSpec(BaseModel):
    """A grouping of fields on a form."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    fields: list[FormFieldSpec] = Field(default_factory=list)
    visible_when: dict[str, Any] | None = None


class FormDefinition(BaseModel):
    """The declarative body of a form."""

    sections: list[FormSectionSpec] = Field(default_factory=list)
    validations: list[dict[str, Any]] = Field(default_factory=list)


class FormVersionResponse(BaseModel):
    """A published form version returned to the client."""

    id: UUID
    tenant_id: UUID
    key: str
    version_number: int
    definition: FormDefinition
    status: str
    published_at: str | None
    published_by: UUID | None
    created_at: str


class FormBindingResponse(BaseModel):
    """A published form binding returned to the client."""

    id: UUID
    tenant_id: UUID
    case_type_version_id: UUID
    form_version_id: UUID
    published_at: str | None
    published_by: UUID | None
    created_at: str


class SubmitFormRequest(BaseModel):
    """POST body for submitting a form against a case."""

    form_key: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


class SubmitFormResponse(BaseModel):
    """Response after submitting a form."""

    case_id: UUID
    reference: str
    version_token: int
    hidden_keys: list[str]


class FormListItem(BaseModel):
    """List item for form versions."""

    id: UUID
    key: str
    version_number: int
    status: str
    published_at: str | None


class FormBindingListItem(BaseModel):
    """List item for form bindings."""

    id: UUID
    case_type_version_id: UUID
    form_version_id: UUID
    published_at: str | None


# ---------------------------------------------------------------------------
# Pagination envelope (AGENTS.md §17.1)
# ---------------------------------------------------------------------------


class Page[T](BaseModel):
    """Cursor-paginated list envelope."""

    items: list[T]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Multi-step / resumable session envelopes (P07-US6 / FM-038)
# ---------------------------------------------------------------------------


class FormStepSpec(BaseModel):
    """Resolved step entry returned to the SPA."""

    step_key: str
    label: str
    section_keys: list[str] = Field(default_factory=list)
    required: bool = True


class StartFormSessionRequest(BaseModel):
    """POST body for starting or resuming a multi-step session."""

    form_key: str = Field(min_length=1, max_length=64)


class SaveFormStepRequest(BaseModel):
    """PATCH body for saving one step's partial data."""

    step_key: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    expected_version_token: int = Field(ge=1)


class FormSessionResponse(BaseModel):
    """Read-side projection of an in-progress form session.

    ``progress`` is a list of ``[step_key, status]`` tuples where
    ``status`` is one of ``pending``, ``current``, ``completed``.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    form_version_id: UUID
    actor_id: UUID
    status: str
    current_step_index: int
    version_token: int
    completed_steps: list[str]
    steps: list[FormStepSpec]
    progress: list[list[str]]
    current_step: FormStepSpec | None
    step_data: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None


class TransitionFormCheckRequest(BaseModel):
    """POST body for the transition form guard (P07-US7).

    ``target_state`` is the state the transition is about to land in;
    ``submitted_form_keys`` is the set of form_keys the case already
    has submitted forms for. The endpoint returns a verdict the
    workflow engine merges with its other guards.

    Why ``target_state`` is in the body: the SPA can call this guard
    before the user clicks "Submit" on a transition form, using the
    state the form will land in. The engine's runtime call site uses
    the same shape — the application validator is purely a function
    over (target_state, requirements, submitted_keys).
    """

    target_state: str = Field(min_length=1, max_length=64)
    submitted_form_keys: list[str] = Field(default_factory=list)


class TransitionFormCheckResponse(BaseModel):
    """Verdict returned by the transition-form guard."""

    allowed: bool
    missing: list[str] = Field(default_factory=list)
    target_state: str | None = None
    evaluated_requirements: list[dict[str, Any]] = Field(default_factory=list)
