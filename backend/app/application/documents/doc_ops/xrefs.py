"""Cross-document references (x-refs) use cases (T5).

Both ends of a reference are resolved in the actor's tenant
(cross-tenant ids surface as ``NotFound``); reading through a
reference still requires ``document.read`` on the target — the
reference never confers access.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.doc_ops import DocXref, sort_xrefs
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

from ._common import (
    DOC_XREF_CREATED,
    SUBJECT_TYPE_DOCUMENT,
    append_audit_and_outbox,
    proxy,
    resolve_document,
)


class XrefStore(Protocol):
    """Persistence port for :class:`DocXref` rows."""

    async def add(self, xref: DocXref) -> None:
        """Persist a freshly created x-ref row."""
        ...

    async def list_for_source(self, tenant_id: UUID, source_document_id: UUID) -> Sequence[DocXref]:
        """Return x-refs citing ``source_document_id`` (tenant-scoped)."""
        ...


class InMemoryXrefStore:
    """Process-local x-ref rows (synthetic tenants; SQL later).

    Tenant isolation is enforced in Python (``WHERE tenant_id`` by
    construction): every read filters on ``tenant_id`` first, so a
    cross-tenant id yields the empty sequence — never a leak.
    """

    def __init__(self) -> None:
        self._rows: list[DocXref] = []

    async def add(self, xref: DocXref) -> None:
        """Append one x-ref row."""
        self._rows.append(xref)

    async def list_for_source(self, tenant_id: UUID, source_document_id: UUID) -> Sequence[DocXref]:
        """Return tenant-scoped x-refs for one source document."""
        return [
            row
            for row in self._rows
            if row.tenant_id == tenant_id and row.source_document_id == source_document_id
        ]

    def clear(self) -> None:
        """Drop all rows (test isolation only)."""
        self._rows.clear()


@dataclass(frozen=True, slots=True)
class CreateXrefCommand:
    """Input to :func:`create_xref`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    source_document_id: UUID
    target_document_id: UUID
    label: str
    sort_order: int = 0


async def create_xref(
    cmd: CreateXrefCommand,
    *,
    document_repo: DocumentRepository,
    xref_store: XrefStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DocXref:
    """Create one cross-document reference (both ends tenant-resolved).

    Steps: authorize ``DOCUMENT_READ`` → resolve both documents in the
    actor's tenant (cross-tenant → ``NotFound``) → build the domain
    aggregate (pure validation: no self-reference) → stage the row →
    audit + outbox. The reference never confers target access.
    """
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=cmd.tenant_id, actor_id=cmd.actor_id, actor_roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.source_document_id),
    )
    source = await resolve_document(
        tenant_id=cmd.tenant_id,
        document_id=cmd.source_document_id,
        document_repo=document_repo,
    )
    target = await resolve_document(
        tenant_id=cmd.tenant_id,
        document_id=cmd.target_document_id,
        document_repo=document_repo,
    )
    now = clock.now()
    xref = DocXref.create(
        tenant_id=cmd.tenant_id,
        source_document_id=cmd.source_document_id,
        target_document_id=cmd.target_document_id,
        label=cmd.label,
        sort_order=cmd.sort_order,
        created_by=cmd.actor_id,
        now=now,
    )
    await xref_store.add(xref)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOC_XREF_CREATED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.source_document_id,
        case_id=source.case_id,
        payload={
            "xref_id": str(xref.id),
            "target_document_id": str(target.id),
            "label": xref.label,
        },
        now=now,
    )
    return xref


async def list_xrefs(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    source_document_id: UUID,
    sort_by: str,
    document_repo: DocumentRepository,
    xref_store: XrefStore,
) -> tuple[DocXref, ...]:
    """List x-refs citing one source document in display order.

    Authorizes ``DOCUMENT_READ`` on the source; the target ids are
    returned as references only (each target read re-authorizes at its
    own boundary).
    """
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=source_document_id),
    )
    await resolve_document(
        tenant_id=tenant_id,
        document_id=source_document_id,
        document_repo=document_repo,
    )
    rows = await xref_store.list_for_source(tenant_id, source_document_id)
    return sort_xrefs(tuple(rows), sort_by=sort_by)
