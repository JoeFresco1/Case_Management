"""Shared helpers for the viewer use cases: actor, correlation, resolve, provenance.

These are the cross-cutting pieces every viewer use case needs, kept in
one module so the responsibility submodules (:mod:`annotation_ops`,
:mod:`checkout_ops`, :mod:`rendition_ops`, :mod:`reindex_ops`) never
import each other in a cycle.

``InvariantViolation`` is re-exported here because the pre-split single
module exposed it on its attribute surface; no helper body raises it —
it exists so callers can catch the domain invariant error without
importing the domain package directly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.application.documents.upload_document import SUBJECT_TYPE_DOCUMENT
from app.domain.context import UserContext
from app.domain.errors import InvariantViolation, NotFound
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter


def _actor(*, tenant_id: UUID, user_id: UUID, roles: tuple[str, ...]) -> UserContext:
    """Build the policy's ``UserContext`` from command actor fields.

    Uses the real domain ``UserContext`` (not a proxy) so ``mypy
    --strict`` checks the ``authorize`` call. ``display_name`` stays
    empty: the policy only reads tenant/roles/system, and names never
    belong in the authorization path (Constitution V).
    """
    return UserContext(user_id=user_id, tenant_id=tenant_id, display_name="", roles=roles)


def _new_correlation_id() -> str:
    """Return a fresh uuid7 string for the audit/outbox correlation id."""
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


async def _resolve_document(
    *, tenant_id: UUID, document_id: UUID, document_repo: DocumentRepository
) -> Any:
    """Resolve a document within the actor's tenant or raise ``NotFound``.

    Cross-tenant ids surface as ``NotFound`` so the API never confirms
    cross-tenant existence (Constitution V).
    """
    document = await document_repo.get(tenant_id, document_id)
    if document is None:
        raise NotFound(f"document {document_id} not found")
    return document


async def _append_audit_and_outbox(
    *,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    tenant_id: UUID,
    event_type: str,
    actor_id: UUID,
    document_id: UUID,
    case_id: UUID | None,
    payload: dict[str, Any],
    now: Any,
) -> None:
    """Append the audit row + outbox row for one viewer mutation.

    The caller stages domain rows first; this helper adds the two
    provenance rows to the same transaction so a partial commit can
    never leave viewer state without an audit trail.
    """
    correlation_id = _new_correlation_id()
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=document_id,
        case_id=case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=uuid4(),
        payload={
            "document_id": str(document_id),
            **({} if case_id is None else {"case_id": str(case_id)}),
            "occurred_at": now.isoformat(),
        },
    )


__all__ = [
    "Any",
    "AuditEventStore",
    "DocumentRepository",
    "InvariantViolation",
    "NotFound",
    "OutboxWriter",
    "SUBJECT_TYPE_DOCUMENT",
    "UUID",
    "UserContext",
    "_actor",
    "_append_audit_and_outbox",
    "_new_correlation_id",
    "_resolve_document",
    "uuid4",
]
