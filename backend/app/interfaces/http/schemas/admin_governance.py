"""Pydantic schemas for the configuration governance API (FM-021).

The governance surface is a thin read model over the
``configuration_publications`` table plus a write seam for the
second-admin approval row (P10-US8). The schemas mirror the
application-layer dataclasses in ``app/application/configuration/
governance.py``; the HTTP layer is intentionally a projection.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

PublicationAction = Literal["draft", "validate", "publish", "retire"]


class PublicationRecordResponse(BaseModel):
    """A single publication-history row (FM-021).

    The wire shape mirrors :class:`PublicationRecord` plus the actor
    fields the admin timeline UI needs (``actor_id`` is sufficient for
    display; a separate ``/users/{id}`` lookup resolves the display
    name).
    """

    id: UUID
    tenant_id: UUID
    artifact_type: str
    artifact_id: UUID | None
    version_id: UUID | None
    action: PublicationAction
    actor_id: UUID
    occurred_at: str
    change_summary: str


class PublicationHistoryResponse(BaseModel):
    """The full governance timeline response.

    ``items`` is newest-first; the standard ``Page[T]`` envelope
    (AGENTS.md §17.1) is not used here because the timeline UI
    intentionally consumes the array directly to render the column.
    """

    items: list[PublicationRecordResponse]


class RecordPublicationRequest(BaseModel):
    """Body for ``POST /api/v1/admin/publications``.

    The router is the durable-write seam for the publish lifecycle
    (P10-US4) and is called by the existing ``publish_case_type`` /
    ``publish_workflow`` use cases after the status flip. The HTTP
    surface is exposed so the admin UI (and tests) can drive the
    approval flow directly.
    """

    artifact_type: str = Field(min_length=1, max_length=64)
    artifact_id: UUID
    version_id: UUID
    action: PublicationAction
    change_summary: str = Field(default="", max_length=4000)


class RecordPublicationResponse(BaseModel):
    publication_id: UUID
    action: PublicationAction
    approval_required: bool


class ApprovePublicationRequest(BaseModel):
    """Body for ``POST /api/v1/admin/publications/approve``.

    The ``approving_actor_id`` is supplied in the body (not pulled
    from headers) so the test surface can drive the approval from a
    different identity than the publish request. In production the
    router derives it from the authenticated user (P11-US5).
    """

    artifact_type: str = Field(min_length=1, max_length=64)
    artifact_id: UUID
    version_id: UUID
    approving_actor_id: UUID
    change_summary: str = Field(default="", max_length=4000)


class ApprovePublicationResponse(BaseModel):
    publication_id: UUID
    action: PublicationAction


__all__ = [
    "ApprovePublicationRequest",
    "ApprovePublicationResponse",
    "PublicationAction",
    "PublicationHistoryResponse",
    "PublicationRecordResponse",
    "RecordPublicationRequest",
    "RecordPublicationResponse",
]
