"""Pydantic schemas for the admin form-binding API (P07 / FM-036).

The admin form-binding router exposes draft/publish/retire operations
on the immutable ``FormBinding`` link between a ``FormVersion`` and a
``CaseTypeVersion``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class StateFormEntry(BaseModel):
    """One state-bound form requirement declared on a binding.

    ``form_key`` names the form that must have been submitted;
    ``state_key`` names the workflow state the requirement guards;
    ``required`` marks whether a transition landing in ``state_key``
    is blocked when the form is missing (P07-US7 / FM-038).

    Why a dedicated model (not ``dict``): malformed entries (blank
    keys, over-long keys, non-bool ``required``) must surface as a
    422 ``ErrorResponse{validation_failed}`` at the HTTP boundary
    before any DB write, never as a silent misconfiguration that
    drifts into the workflow engine.
    """

    form_key: str = Field(min_length=1, max_length=64)
    state_key: str = Field(min_length=1, max_length=64)
    required: bool = True


class CreateFormBindingRequest(BaseModel):
    """Body for ``POST /admin/form-bindings``."""

    case_type_version_id: UUID
    form_version_id: UUID
    state_forms: list[StateFormEntry] = Field(default_factory=list)


class FormBindingDetailResponse(BaseModel):
    """Full detail of one form binding."""

    id: UUID
    tenant_id: UUID
    case_type_version_id: UUID
    form_version_id: UUID
    published_at: str | None = None
    published_by: UUID | None = None
    created_at: str
    state_forms: list[dict[str, Any]] = Field(default_factory=list)
    retired_at: str | None = None
    retired_by: UUID | None = None


class Page[T](BaseModel):
    """Cursor-paginated list envelope (AGENTS.md §17.1).

    ``limit`` defaults to 50 and caps at 200; ``next_cursor`` is an
    opaque ``<offset>:<limit>`` string the caller re-issues as query
    params. An absent cursor means the last page.
    """

    items: list[T]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)


__all__ = [
    "CreateFormBindingRequest",
    "FormBindingDetailResponse",
    "Page",
    "StateFormEntry",
]
