"""Renditions: redaction burn as new bytes (original preserved) + unified revision history.

Renditions are burned viewer bytes (redacted burns) stored via
``DocumentStore.put`` as *new* content; the original version rows stay
untouched so the original remains retrievable. :func:`revision_history`
merges the immutable filed versions and the burned renditions into one
timeline so workers see "v2 filed, redacted burn of v2, v3 filed"
without joining two endpoints.

# Failure semantics

Auth / not-found / validation errors raise before any write. On
``DocumentStore.put`` raising ``DependencyUnavailable`` the caller
rolls the transaction back; no audit row is appended.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.documents.upload_document import SUBJECT_TYPE_DOCUMENT
from app.domain.annotations import Rendition, RenditionKind
from app.domain.document import DocumentVersion
from app.domain.ports import (
    AuditEventStore,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
    RenditionRepository,
)

from .events import RENDITION_BURNED
from .helpers import _actor, _new_correlation_id, _resolve_document


@dataclass(frozen=True, slots=True)
class BurnRenditionCommand:
    """Input to :func:`burn_rendition`.

    ``content`` is the burned bytes (e.g. a PDF with the approved
    regions blacked out), rendered by the viewer client. The use case
    stores them as *new* content; the source version rows are never
    modified.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    source_version: int
    content: bytes
    content_type: str
    reason: str
    region: str = ""


async def burn_rendition(
    cmd: BurnRenditionCommand,
    *,
    document_repo: DocumentRepository,
    rendition_repo: RenditionRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> Rendition:
    """Burn approved redaction bytes as a new ``REDACTED`` rendition.

    Steps: authorize ``DOCUMENT_REDACT`` → resolve the document →
    refuse source versions outside ``1..current_version`` → store the
    bytes via ``DocumentStore.put`` → shape the rendition descriptor
    through the domain propose/apply/burn flow → stage the row → audit
    + outbox.

    Why the in-use-case propose/apply/burn: the burn itself *is* the
    approval (an authenticated ``DOCUMENT_REDACT`` action, audited as
    ``RenditionBurned`` with reason + region). The ephemeral mark links
    the rendition back to its rationale via ``redaction_id``; a
    persisted multi-step mark workflow is a future extension that
    reuses the same descriptor without changing this contract.
    """
    await authorize(
        Action.DOCUMENT_REDACT,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    if cmd.source_version < 1 or cmd.source_version > document.current_version:
        from app.domain.errors import ValidationFailed  # noqa: PLC0415

        raise ValidationFailed(
            f"source_version {cmd.source_version} is not in the "
            f"document's version history (1..{document.current_version})",
            field="source_version",
        )
    import hashlib  # noqa: PLC0415

    now = clock.now()
    sha256 = hashlib.sha256(cmd.content).hexdigest()
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.content,
        content_type=cmd.content_type,
        sha256=sha256,
    )
    # why: route the burn through the domain mark lifecycle so the
    # "only applied marks burn" invariant is exercised on every burn,
    # not just in domain unit tests.
    from app.domain.annotations import Redaction  # noqa: PLC0415

    mark = Redaction.propose(
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        base_version=cmd.source_version,
        page=1,
        region=cmd.region.strip() or f"version {cmd.source_version} burn",
        reason=cmd.reason,
        author_id=cmd.actor_id,
        now=now,
    ).apply(now=now)
    rendition = mark.burn_as_rendition(
        sha256=sha256,
        size=len(cmd.content),
        storage_ref=storage_ref,
        content_type=cmd.content_type,
        now=now,
    )
    await rendition_repo.add(rendition)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=RENDITION_BURNED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=document.id,
        case_id=document.case_id,
        correlation_id=_new_correlation_id(),
        causation_id=None,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "rendition_id": str(rendition.id),
            "source_version": cmd.source_version,
            "kind": RenditionKind.REDACTED.value,
            "sha256": sha256,
            "size": len(cmd.content),
            "content_type": cmd.content_type,
            "storage_ref": storage_ref,
            "reason": mark.reason,
            "region": mark.region,
            "redaction_id": str(mark.id),
            "original_sha256": document.sha256,
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=RENDITION_BURNED,
        event_id=uuid4(),
        payload={
            "document_id": str(document.id),
            "case_id": str(document.case_id),
            "rendition_id": str(rendition.id),
            "source_version": cmd.source_version,
            "occurred_at": now.isoformat(),
        },
    )
    return rendition


@dataclass(frozen=True, slots=True)
class RevisionEntry:
    """One row of the unified revision/rendition history view."""

    entry_type: str  # "version" | "rendition"
    version_number: int
    kind: str  # version: "original"; rendition: RenditionKind value
    sha256: str
    size: int
    occurred_at: Any
    rendition_id: UUID | None = None
    redaction_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RevisionHistory:
    """Unified revision + rendition history for a document.

    ``versions`` are the filed ``DocumentVersion`` rows (originals,
    immutable); ``renditions`` are burned viewer bytes. The viewer
    renders them as one timeline so workers see "v2 filed, redacted
    burn of v2, v3 filed" without joining two endpoints.
    """

    document_id: UUID
    versions: tuple[DocumentVersion, ...]
    renditions: tuple[Rendition, ...]

    def entries(self) -> tuple[RevisionEntry, ...]:
        """Merge versions + renditions oldest-first for the timeline."""
        merged: list[RevisionEntry] = [
            RevisionEntry(
                entry_type="version",
                version_number=v.version_number,
                kind="original",
                sha256=v.sha256,
                size=v.size,
                occurred_at=v.uploaded_at,
            )
            for v in self.versions
        ]
        merged.extend(
            RevisionEntry(
                entry_type="rendition",
                version_number=r.source_version,
                kind=r.kind.value,
                sha256=r.sha256,
                size=r.size,
                occurred_at=r.created_at,
                rendition_id=r.id,
                redaction_id=r.redaction_id,
            )
            for r in self.renditions
        )
        merged.sort(key=lambda e: (str(e.occurred_at), e.entry_type))
        return tuple(merged)


async def revision_history(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    document_id: UUID,
    document_repo: DocumentRepository,
    rendition_repo: RenditionRepository,
) -> RevisionHistory:
    """Return the unified revision/rendition history (read side).

    Authorized by ``DOCUMENT_READ``; tenant-scoped; cross-tenant ids
    surface as ``NotFound``.
    """
    await authorize(
        Action.DOCUMENT_READ,
        _actor(tenant_id=tenant_id, user_id=actor_id, roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=document_id),
    )
    document = await _resolve_document(
        tenant_id=tenant_id, document_id=document_id, document_repo=document_repo
    )
    versions = await document_repo.list_versions(tenant_id, document.id)
    renditions = await rendition_repo.list_for_document(tenant_id, document.id)
    return RevisionHistory(
        document_id=document.id,
        versions=tuple(versions),
        renditions=tuple(renditions),
    )


__all__ = [
    "BurnRenditionCommand",
    "DocumentStore",
    "DocumentVersion",
    "Rendition",
    "RenditionKind",
    "RenditionRepository",
    "RevisionEntry",
    "RevisionHistory",
    "burn_rendition",
    "revision_history",
]
