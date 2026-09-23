"""``ReindexRequest`` — the viewer "re-index" action as a validated value object.

The viewer exposes "re-index" after edits/imports so full-text and
metadata catalogs converge. This value object validates *what* to
re-index and *why* (a non-blank reason bounded by ``MAX_TEXT_LENGTH``);
the rebuild itself runs behind the search-index adapter seam (out of
scope here), audited as ``DocumentReindexed`` by the use case (R11 —
pure domain, no I/O, no vendor SDK).

``# INTEGRATION_SEAM``: the rebuild is a future search-index adapter
local/Synthetic today, Azure AI Search later; the domain only shapes
the request.

Symbols: ``ReindexRequest``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import ValidationFailed

from .constants import MAX_TEXT_LENGTH


@dataclass(frozen=True, slots=True)
class ReindexRequest:
    """A validated request to rebuild a document's search index entry.

    The viewer exposes "re-index" after edits/imports so full-text and
    metadata catalogs converge. This value object validates *what* to
    re-index and *why*; the rebuild itself runs behind the search-index
    adapter seam (out of scope here), audited as ``DocumentReindexed``.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    requested_by: UUID
    reason: str
    requested_at: datetime

    def __post_init__(self) -> None:
        """Validate the request rationale (pure, no I/O)."""
        if not self.reason or not self.reason.strip():
            raise ValidationFailed("reason is required", field="reason")
        if len(self.reason) > MAX_TEXT_LENGTH:
            raise ValidationFailed(f"reason must be <= {MAX_TEXT_LENGTH} chars", field="reason")

    @staticmethod
    def request(
        *,
        tenant_id: UUID,
        document_id: UUID,
        requested_by: UUID,
        reason: str,
        now: datetime,
    ) -> ReindexRequest:
        """Construct a validated re-index request."""
        return ReindexRequest(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            requested_by=requested_by,
            reason=reason.strip(),
            requested_at=now,
        )


__all__ = [
    "MAX_TEXT_LENGTH",
    "ReindexRequest",
    "UUID",
    "ValidationFailed",
    "dataclass",
    "datetime",
    "uuid4",
]
