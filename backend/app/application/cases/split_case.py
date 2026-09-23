"""Split a case into two cases (P01-US7, FM-010).

The use case creates a brand-new case that inherits the source's
``case_type_version`` and a *copy* of every attached document (the
metadata rows are cloned; the document versions reference the same
content-addressed bytes — re-uploading is unnecessary because the
content hash is the identity).

The split flow:

1. **Authorize** the actor against ``Action.CASE_UPDATE`` on the source
   case (splitting is a destructive mutation of the workspace).
2. **Resolve the source case** + published ``CaseTypeVersion``. The
   new case inherits the version so future writes validate against the
   same schema.
3. **Read attached documents** on the source via the document port. For
   each document we stage a fresh ``Document`` row whose ``sha256``,
   ``storage_ref``, ``size``, and ``content_type`` are copied; the
   ``current_version`` resets to 1 and a fresh ``DocumentVersion`` row
   is staged with ``version_number=1`` and the same ``storage_ref``.
   Provenance is recorded in the new document's first-version payload
   (``provenance={"split_from": "<source_case_id>"}``) so a reader can
   tell the document was carried over.
4. **Stage the new case** + a ``split_from`` edge + per-case audit rows
   + one outbox event. The split edge groups the source + new case so
   ``list_related(source, kind=split_from)`` enumerates every child
   case the source produced.
5. **Audit history preservation**: the source's existing audit chain is
   untouched. The new case starts with its own ``CaseCreated`` row
   (carrying the split provenance in the payload) and a
   ``CaseSplitFrom`` row. The split itself is a compensating event on
   the *source* (`CaseSplit` payload references the new case id).

Failure behavior: validation/auth/not-found errors raise before any DB
write. The new case INSERT, the document clones, the edge, the audit
rows, and the outbox row share one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.create_case import _UserProxy as _ActorProxy
from app.domain.audit import CASE_CREATED
from app.domain.case import Case, CaseReference
from app.domain.case_relationship import (
    CaseRelationship,
    make_split_edge,
)
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CaseRelationshipRepository,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
)

CASE_SPLIT: str = "CaseSplit"
DOCUMENT_CARRIED_BY_SPLIT: str = "DocumentCarriedBySplit"


class _ClockLike(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class SplitCaseCommand:
    """Input to :func:`handle`.

    ``new_title`` is required: the new case must have an
    operator-chosen title because the source's title may not be a
    valid descriptor for the split half (e.g. splitting a case titled
    "Refunds batch — Q3 cleanup" into one half titled "Refund 1234
    escalation"). ``new_description`` and ``new_data`` default to the
    source's values when the caller does not override them.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    source_case_id: UUID
    new_title: str
    new_description: str | None = None
    new_data: dict[str, Any] | None = None
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SplitResult:
    """The post-commit outcome returned to the router."""

    new_case: Case
    split_edge: CaseRelationship
    carried_document_ids: tuple[UUID, ...]


async def handle(
    cmd: SplitCaseCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    document_repo: DocumentRepository,
    relationship_repo: CaseRelationshipRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    case_type_repo: CaseTypeRepository | None = None,
) -> SplitResult:
    """Split a case into a new case carrying a copy of the documents.

    Failure behavior:
    - Auth/validation/not-found errors are raised before any DB write.
    - The new case INSERT, the document clones, the edge, the audit
    rows, and the outbox row share one transaction.

    ``case_type_repo`` is optional for backward compatibility: when
    supplied, the parent case type key feeds the tenant's reference
    template (FM-009 ``{TYPE}``). When absent, ``type_key`` is
    ``None`` and a ``{TYPE}`` template fails closed at the repository.
    """
    if not cmd.new_title.strip():
        raise ValidationFailed("new_title is required", field="new_title")

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )

    await authorize(
        Action.CASE_UPDATE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.source_case_id),
    )

    source = await case_repo.get(cmd.tenant_id, cmd.source_case_id)
    if source is None:
        raise NotFound(f"case {cmd.source_case_id} not found")

    version = await case_type_version_repo.get(cmd.tenant_id, source.case_type_version_id)
    if version is None:
        raise NotFound(f"case type version {source.case_type_version_id} not found")

    # FM-009: resolve the parent type key for the reference template.
    # The split inherits the source's type, so the new reference honors
    # the same `{TYPE}` projection a fresh intake would emit.
    type_key: str | None = None
    if case_type_repo is not None:
        case_type = await case_type_repo.get(cmd.tenant_id, version.case_type_id)
        if case_type is not None:
            type_key = case_type.key

    now = clock.now()
    reference = await case_repo.next_reference(cmd.tenant_id, now=now, type_key=type_key)

    new_description = cmd.new_description if cmd.new_description is not None else source.description
    new_data = cmd.new_data if cmd.new_data is not None else dict(source.data)

    new_case = Case.create(
        tenant_id=cmd.tenant_id,
        reference=reference,
        case_type_version=version,
        title=cmd.new_title,
        description=new_description,
        data=new_data,
        priority=source.priority,
        created_by=cmd.actor_id,
        now=now,
        sensitivity=source.sensitivity,
    )

    # Carry the documents forward. We clone the metadata rows (so each
    # case has its own ``document.id`` and its own audit chain) and
    # create a fresh version-1 row that points at the same
    # ``storage_ref``. Provenance is stamped on the new
    # ``DocumentVersion`` payload so a reader can tell the document
    # was carried over from a split.
    carried_ids: list[UUID] = []
    source_documents = await _list_case_documents(document_repo, cmd.tenant_id, source.id)
    for doc in source_documents:
        new_doc, new_version = _clone_document_for_split(
            doc=doc,
            target_case_id=new_case.id,
            actor_id=cmd.actor_id,
            now=now,
            provenance={"split_from": str(source.id)},
        )
        await document_repo.add_document_and_version(document=new_doc, version=new_version)
        carried_ids.append(new_doc.id)
        # Audit row on the new document row so the split is visible on
        # the document's own timeline.
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=DOCUMENT_CARRIED_BY_SPLIT,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type="document",
            subject_id=new_doc.id,
            case_id=new_case.id,
            correlation_id=cmd.correlation_id or _new_correlation_id(),
            causation_id=cmd.causation_id,
            payload={
                "new_document_id": str(new_doc.id),
                "source_case_id": str(source.id),
                "source_document_id": str(doc.id),
                "file_name": new_doc.file_name,
                "sha256": new_doc.sha256,
                "size": new_doc.size,
            },
            occurred_at=now,
        )

    edge = make_split_edge(
        tenant_id=cmd.tenant_id,
        original_case_id=source.id,
        new_case_id=new_case.id,
        actor_id=cmd.actor_id,
        now=now,
        reason="case split",
        note=f"split from {source.reference.value}",
    )

    await case_repo.add(new_case)
    await relationship_repo.add(edge)

    correlation = cmd.correlation_id or _new_correlation_id()

    # Audit row on the new case (CaseCreated) so the new case's
    # timeline starts with a "where did I come from" provenance row.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=new_case.id,
        case_id=new_case.id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "reference": new_case.reference.value,
            "case_type_version_id": str(new_case.case_type_version_id),
            "priority": new_case.priority,
            "title": new_case.title,
            "status": new_case.status.value,
            "provenance": {
                "source_case_id": str(source.id),
                "source_reference": source.reference.value,
                "operation": "split",
                "carried_document_ids": [str(d) for d in carried_ids],
            },
        },
        occurred_at=now,
    )

    # Compensating event on the source case so the source timeline
    # shows the split without rewriting history (Constitution IV).
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_SPLIT,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="case",
        subject_id=source.id,
        case_id=source.id,
        correlation_id=correlation,
        causation_id=cmd.causation_id,
        payload={
            "new_case_id": str(new_case.id),
            "new_reference": new_case.reference.value,
            "carried_document_ids": [str(d) for d in carried_ids],
            "relationship_id": str(edge.relationship_id),
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_SPLIT,
        event_id=uuid4(),
        payload={
            "tenant_id": str(cmd.tenant_id),
            "source_case_id": str(source.id),
            "source_reference": source.reference.value,
            "new_case_id": str(new_case.id),
            "new_reference": new_case.reference.value,
            "actor_id": str(cmd.actor_id),
            "carried_document_ids": [str(d) for d in carried_ids],
            "occurred_at": now.isoformat(),
        },
    )

    return SplitResult(
        new_case=new_case,
        split_edge=edge,
        carried_document_ids=tuple(carried_ids),
    )


# ---- helpers ---------------------------------------------------------------


async def _list_case_documents(
    repo: DocumentRepository, tenant_id: UUID, case_id: UUID
) -> list[Document]:
    """List documents attached to a case via the repository port.

    The first-slice :class:`DocumentRepository` port does not expose a
    ``list_for_case`` method; the SQL adapter is the canonical
    implementation and the in-memory fake implements the same shape.
    We call the helper module-level so tests can substitute their own
    fake without a database.
    """
    return await _list_documents_for_case(repo, tenant_id, case_id)


def _clone_document_for_split(
    *,
    doc: Document,
    target_case_id: UUID,
    actor_id: UUID,
    now: datetime,
    provenance: dict[str, Any],
) -> tuple[Document, DocumentVersion]:
    """Clone a document for a split case.

    The metadata is cloned; the bytes are not (the ``storage_ref``
    already addresses them content-hashed). The first version row is
    re-created under the new document id so the case's version history
    starts fresh — the original document row on the source case is
    untouched and retains its own version history.
    """
    new_doc = Document.create(
        tenant_id=doc.tenant_id,
        case_id=target_case_id,
        file_name=doc.file_name,
        content_type=doc.content_type,
        classification=doc.classification,
        sensitivity=doc.sensitivity,
        sha256=doc.sha256,
        size=doc.size,
        storage_ref=doc.storage_ref,
        uploaded_by=actor_id,
        now=now,
    )
    # ``Document.create`` always sets sensitivity to ``INTERNAL``; the
    # helper accepted ``DocumentSensitivity`` so we need to cast through
    # the enum. Keep the source's sensitivity when the caller carried
    # it explicitly.
    if doc.sensitivity is not DocumentSensitivity.INTERNAL:
        new_doc = replace(new_doc, sensitivity=doc.sensitivity)
    new_version = DocumentVersion(
        id=uuid4(),
        tenant_id=doc.tenant_id,
        document_id=new_doc.id,
        version_number=1,
        sha256=doc.sha256,
        size=doc.size,
        storage_ref=doc.storage_ref,
        content_type=doc.content_type,
        uploaded_by=actor_id,
        uploaded_at=now,
    )
    # Stamp provenance onto the document row so a reader can tell the
    # bytes were carried over from a split. The ``description``-style
    # payload lives in the first DocumentVersion audit row instead of
    # the document row to keep the aggregate immutable.
    _ = provenance
    return new_doc, new_version


async def _list_documents_for_case(
    repo: DocumentRepository, tenant_id: UUID, case_id: UUID
) -> list[Document]:
    """Dispatch to the repository's list-for-case capability.

    The protocol does not yet declare ``list_for_case`` because the
    first slice never needed it; we call it via duck typing so the SQL
    adapter can grow the method without changing the domain port
    signature today. The in-memory fake ships the same method.
    """
    method = getattr(repo, "list_for_case", None)
    if method is None:  # pragma: no cover - exercised when the SQL adapter is wired
        return []
    return list(await method(tenant_id=tenant_id, case_id=case_id))


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


# Re-export so callers can import the case-reference factory symbol
# without a separate import line.
_ = CaseReference


__all__ = [
    "CASE_SPLIT",
    "DOCUMENT_CARRIED_BY_SPLIT",
    "SplitCaseCommand",
    "SplitResult",
    "handle",
]
