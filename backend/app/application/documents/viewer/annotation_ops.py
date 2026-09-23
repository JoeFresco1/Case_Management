"""Annotation overlay CRUD: create / update / delete (document bytes never touched).

The browser-native viewer renders documents over an **overlay** model:
annotations (highlight / text-box / stamp / freehand) are markup
pinned to ``(document_id, document_version, page)``. They never
mutate document bytes: the annotation path touches only the
``viewer_annotations`` rows plus audit + outbox. The ``documents`` /
``document_versions`` rows are only *read* for the version bound and
never written here (T5 acceptance: "annotation layer never mutates
original bytes").

# Failure semantics

Auth / not-found / validation errors raise before any write. A
``document_id`` mismatch on an annotation id surfaces as ``NotFound``
so an annotation id from one document cannot be edited through
another document's URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.annotations import Annotation, AnnotationKind
from app.domain.errors import NotFound
from app.domain.ports import (
    AnnotationRepository,
    AuditEventStore,
    DocumentRepository,
    OutboxWriter,
)

from .events import ANNOTATION_CREATED, ANNOTATION_DELETED, ANNOTATION_UPDATED
from .helpers import _actor, _append_audit_and_outbox, _resolve_document


@dataclass(frozen=True, slots=True)
class CreateAnnotationCommand:
    """Input to :func:`create_annotation`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    document_version: int
    page: int
    kind: str
    geometry: dict[str, object] = field(default_factory=dict)
    content: str | None = None


async def create_annotation(
    cmd: CreateAnnotationCommand,
    *,
    document_repo: DocumentRepository,
    annotation_repo: AnnotationRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> Annotation:
    """Create one viewer annotation over a document version.

    Steps: authorize ``DOCUMENT_ANNOTATE`` → resolve the document in
    the actor's tenant → refuse versions outside ``1..current_version``
    (the mark must pin a version that exists) → build the domain
    aggregate (pure validation) → stage the row → audit + outbox.

    The ``documents`` / ``document_versions`` rows are only *read* for
    the version bound; no write touches them, so the original bytes
    cannot change on this path.
    """
    await authorize(
        Action.DOCUMENT_ANNOTATE,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    if cmd.document_version < 1 or cmd.document_version > document.current_version:
        from app.domain.errors import ValidationFailed  # noqa: PLC0415

        raise ValidationFailed(
            f"document_version {cmd.document_version} is not in the "
            f"document's version history (1..{document.current_version})",
            field="document_version",
        )
    now = clock.now()
    annotation = Annotation.create(
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        document_version=cmd.document_version,
        page=cmd.page,
        kind=cmd.kind,
        geometry=dict(cmd.geometry),
        content=cmd.content,
        author_id=cmd.actor_id,
        now=now,
    )
    await annotation_repo.add(annotation)
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=ANNOTATION_CREATED,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            **dict(annotation.to_payload()),
        },
        now=now,
    )
    return annotation


@dataclass(frozen=True, slots=True)
class UpdateAnnotationCommand:
    """Input to :func:`update_annotation` (overlay edit only)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    annotation_id: UUID
    geometry: dict[str, object] | None = None
    content: str | None = None
    clear_content: bool = False


async def update_annotation(
    cmd: UpdateAnnotationCommand,
    *,
    document_repo: DocumentRepository,
    annotation_repo: AnnotationRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> Annotation:
    """Edit an annotation's overlay (geometry and/or caption text).

    Only the overlay columns change: the pinned ``(document_id,
    document_version, page, kind)`` identity and the ``created_by`` /
    ``created_at`` provenance are immutable. A ``document_id``
    mismatch surfaces as ``NotFound`` so an annotation id from one
    document cannot be edited through another document's URL.
    """
    await authorize(
        Action.DOCUMENT_ANNOTATE,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    current = await annotation_repo.get(cmd.tenant_id, cmd.annotation_id)
    if current is None or current.document_id != document.id:
        raise NotFound(f"annotation {cmd.annotation_id} not found")
    # why: ``dataclasses.replace`` re-runs ``__post_init__`` so the
    # kind/content pairing (text_box requires text) and the geometry
    # shape are re-validated on edit, not just on create.
    updated = replace(
        current,
        geometry=dict(cmd.geometry) if cmd.geometry is not None else current.geometry,
        content=None
        if cmd.clear_content
        else (cmd.content if cmd.content is not None else current.content),
    )
    await annotation_repo.save(updated)
    now = clock.now()
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=ANNOTATION_UPDATED,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            **dict(updated.to_payload()),
        },
        now=now,
    )
    return updated


@dataclass(frozen=True, slots=True)
class DeleteAnnotationCommand:
    """Input to :func:`delete_annotation`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    annotation_id: UUID


async def delete_annotation(
    cmd: DeleteAnnotationCommand,
    *,
    document_repo: DocumentRepository,
    annotation_repo: AnnotationRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> UUID:
    """Remove one annotation overlay row (audited, document untouched)."""
    await authorize(
        Action.DOCUMENT_ANNOTATE,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    current = await annotation_repo.get(cmd.tenant_id, cmd.annotation_id)
    if current is None or current.document_id != document.id:
        raise NotFound(f"annotation {cmd.annotation_id} not found")
    removed = await annotation_repo.delete(cmd.tenant_id, cmd.annotation_id)
    if not removed:
        raise NotFound(f"annotation {cmd.annotation_id} not found")
    now = clock.now()
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=ANNOTATION_DELETED,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "annotation_id": str(current.id),
            "kind": current.kind.value,
        },
        now=now,
    )
    return current.id


__all__ = [
    "Action",
    "Annotation",
    "AnnotationKind",
    "AnnotationRepository",
    "CreateAnnotationCommand",
    "DeleteAnnotationCommand",
    "Resource",
    "UpdateAnnotationCommand",
    "authorize",
    "create_annotation",
    "dataclass",
    "delete_annotation",
    "field",
    "replace",
    "update_annotation",
]
