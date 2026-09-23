"""Re-index: validated request recorded with audit + outbox (rebuild behind the search seam).

# INTEGRATION_SEAM: search index

``request_reindex`` records the validated request + audit + outbox.
A future search-index adapter subscribes to the ``DocumentReindexed``
outbox event to rebuild the full-text/metadata catalog. The portable
``LIKE`` provider needs no rebuild, so local behavior is already
convergent; the event exists so Azure Cognitive Search can converge
later without a contract change.

# Failure semantics

Auth / not-found / validation errors raise before any write; the
request itself is the durable artifact (validated reason +
``DocumentReindexed`` audit + outbox row in one transaction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.annotations import ReindexRequest
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

from .events import DOCUMENT_REINDEXED
from .helpers import _actor, _append_audit_and_outbox, _resolve_document


@dataclass(frozen=True, slots=True)
class ReindexCommand:
    """Input to :func:`request_reindex`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    reason: str


async def request_reindex(
    cmd: ReindexCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> ReindexRequest:
    """Validate and record a document re-index request.

    The request itself is the durable artifact (validated reason +
    ``DocumentReindexed`` audit + outbox). The rebuild runs behind the
    search-index seam subscribed to the outbox event; the portable
    ``LIKE`` provider converges without a rebuild, so local reads are
    already correct and the event is the Azure-search convergence
    hook (see module docstring).
    """
    await authorize(
        Action.DOCUMENT_REINDEX,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    now = clock.now()
    request = ReindexRequest.request(
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        requested_by=cmd.actor_id,
        reason=cmd.reason,
        now=now,
    )
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_REINDEXED,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "reindex_request_id": str(request.id),
            "reason": request.reason,
            "current_version": document.current_version,
        },
        now=now,
    )
    return request


__all__ = [
    "ReindexCommand",
    "ReindexRequest",
    "request_reindex",
]
