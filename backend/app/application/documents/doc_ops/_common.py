"""Shared helpers for document-operations use cases (T5).

This module owns the cross-cutting pieces used by the ``doc_ops``
subpackage: stable event-type literals, subject-type literals, the
policy-user proxy, tenant-scoped document resolution, the non-raising
grant probe, and the audit + outbox appender that keeps every
mutation audit-atomic (FR-L02).

# Why a separate module

The former single ``doc_ops.py`` (1359 lines) exceeded the R2
1000-line hard cap. Splitting by responsibility (x-refs, separation,
packages, bundles) requires one shared home for these helpers so
the submodules do not import each other in a cycle.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

# Stable event types. Subscribers depend on these literals.
DOC_XREF_CREATED = "DocXrefCreated"
SEPARATION_PLANNED = "SeparationPlanned"
DOC_PACKAGE_BUILT = "DocPackageBuilt"
CSV_EXPORTED = "CsvExported"
DOC_BUNDLE_CREATED = "DocBundleCreated"
DOC_BUNDLE_MEMBERS_CHANGED = "DocBundleMembersChanged"
DOC_BUNDLE_REVOKED = "DocBundleRevoked"
SHARE_LINK_MINTED = "ShareLinkMinted"
SHARE_LINK_REDEEMED = "ShareLinkRedeemed"
SHARE_LINK_REVOKED = "ShareLinkRevoked"

SUBJECT_TYPE_DOCUMENT = "document"
SUBJECT_TYPE_BUNDLE = "doc_bundle"


def proxy(*, tenant_id: UUID, actor_id: UUID, actor_roles: tuple[str, ...]) -> UserContext:
    """Build the policy user for one actor.

    Uses the framework-neutral ``app.domain.context.UserContext``
    directly (no FastAPI ``app.deps`` import, per AGENTS.md R10).
    """
    return UserContext(
        user_id=actor_id,
        tenant_id=tenant_id,
        display_name="",
        roles=actor_roles,
    )


async def resolve_document(
    *, tenant_id: UUID, document_id: UUID, document_repo: DocumentRepository
) -> Any:
    """Resolve a document in the actor's tenant or raise ``NotFound``.

    Cross-tenant ids surface as ``NotFound`` so the API never confirms
    cross-tenant existence (Constitution V).
    """
    from app.domain.errors import NotFound  # noqa: PLC0415

    document = await document_repo.get(tenant_id, document_id)
    if document is None:
        raise NotFound(f"document {document_id} not found")
    return document


async def has_action(
    action: Action,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    resource_id: UUID | None = None,
) -> bool:
    """Return True iff the policy grants ``action`` (never raises)."""
    from app.domain.errors import Forbidden  # noqa: PLC0415

    try:
        await authorize(
            action,
            proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
            Resource(
                tenant_id=tenant_id,
                kind="document",
                id=resource_id,
            ),
        )
    except Forbidden:
        return False
    return True


async def append_audit_and_outbox(
    *,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    tenant_id: UUID,
    event_type: str,
    actor_id: UUID,
    subject_type: str,
    subject_id: UUID,
    case_id: UUID | None,
    payload: dict[str, Any],
    now: Any,
) -> None:
    """Append the audit row + outbox row for one doc-ops mutation.

    The caller stages domain rows first; this helper adds the two
    provenance rows to the same transaction so a partial commit can
    never leave doc-ops state without an audit trail (FR-L02).
    """
    from uuid_extensions import uuid7  # noqa: PLC0415

    correlation_id = str(uuid7())
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type=subject_type,
        subject_id=subject_id,
        case_id=case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=uuid7(),
        payload={
            "subject_id": str(subject_id),
            **({} if case_id is None else {"case_id": str(case_id)}),
            "occurred_at": now.isoformat(),
        },
    )
