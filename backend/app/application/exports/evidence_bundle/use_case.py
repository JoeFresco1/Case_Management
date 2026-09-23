"""Build the portable evidence bundle use case (P14-US8 / FM-060).

Orchestrates the export end to end: validate the command, authorize the
case and document reads through the deny-by-default policy, resolve the
case and its case-type version (cross-tenant probes surface as
``NotFound``), read every document's bytes through the ``DocumentStore``
seam, project the redacted case/audit/document payloads, assemble the
manifest plus both archive representations, and record the export action
as its own audit event in the caller's transaction.

See the package docstring (``app.application.exports.evidence_bundle``)
for the full authorization, redaction, failure-semantics, and
integration-seam story. Split out of the former single
``evidence_bundle`` module by the R2 structural refactor; everything
here is re-exported from ``app.application.exports.evidence_bundle``
unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.audit.record_event import (
    RecordAuditCommand,
    record_event,
)
from app.application.authorization import Action, Resource, authorize
from app.domain.audit import (
    payload_hash as _payload_hash,
)
from app.domain.document import Document
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    DocumentStore,
)

from .archive import (
    _build_archive,
    _build_entry_list,
    _manifest_entry,
)
from .constants import (
    AUDIT_EVIDENCE_BUNDLE_EXPORTED,
    BUNDLE_SCHEMA_VERSION,
    EXPORT_KEY_AUDIT_ROW_COUNT,
    EXPORT_KEY_BUNDLE,
    EXPORT_KEY_CASE_ID,
    EXPORT_KEY_DOCUMENT_COUNT,
    EXPORT_KEY_MANIFEST_HASH,
    EXPORT_KEY_REASON,
)
from .helpers import (
    _ActorProxy,
    _audit_row_to_dict,
    _canonical_json,
    _case_to_dict,
    _document_to_dict,
    _list_documents_for_case,
    _now,
    _now_iso,
    _redact_case_data,
    _sha256_hex,
    _validate_command,
)
from .types import BuildEvidenceBundleCommand, EvidenceBundle

# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    cmd: BuildEvidenceBundleCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    dedupe: Any | None = None,
    clock: Any | None = None,
) -> EvidenceBundle:
    """Build the portable evidence bundle for ``cmd.case_id``.

    The flow is:

    1. Validate the command and authorize the case read.
    2. Resolve the case (cross-tenant probes surface as ``NotFound``).
    3. Resolve the case-type version to consult the redaction
       allowlist.
    4. Authorize the document read in aggregate (``DOCUMENT_READ``
       on the case resource is sufficient for the first slice; the
       per-document policy is the same for every document on a
       case).
    5. Fetch every document via the ``DocumentRepository.list_for_case``
       hook and read its bytes through the ``DocumentStore``. A
       document the actor cannot read surfaces as ``Forbidden``
       before any archive entry is staged.
    6. Fetch the audit timeline through ``list_for_case`` and
       redaction-strip every payload.
    7. Build the manifest, the ZIP archive, and the audit event that
       records the export action.
    """
    _validate_command(cmd)

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )

    # 1. Authorize the case read. The resource is the case; the
    # policy evaluates ``Action.CASE_READ`` against the actor's role
    # set. A denied read raises ``Forbidden``; the HTTP layer maps
    # it to 403.
    await authorize(
        Action.CASE_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    # 2. Resolve the case. Cross-tenant ids resolve to ``None``
    # (the repository filters by tenant_id) and the use case raises
    # ``NotFound`` so the API never confirms cross-tenant existence
    # (Constitution V).
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # 3. Resolve the case-type version. The redaction allowlist is
    # the schema's ``sensitive_field_keys`` set; without the
    # version the redactor cannot know which keys to redact.
    version = await case_type_version_repo.get(cmd.tenant_id, case.case_type_version_id)
    if version is None:
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    # 4. Authorize the document read. The resource is still the
    # case (the policy today does not carry a per-document read
    # action; the case scope is sufficient because every document
    # on a case shares the same classification policy).
    await authorize(
        Action.DOCUMENT_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    # 5. Fetch every document on the case. The use case requires
    # the same ``list_for_case`` hook the GraphQL governed surface
    # uses (see ``app.application.documents.list_documents``); the
    # application layer never invents its own tenant-wide listing.
    documents = await _list_documents_for_case(document_repo, cmd.tenant_id, case.id)

    document_entries: list[tuple[Document, bytes]] = []
    for document in documents:
        # 5a. Read the bytes through the DocumentStore. A storage
        # failure surfaces as ``DependencyUnavailable`` and is
        # propagated unchanged — the use case refuses to ship a
        # partial archive.
        content = await document_store.get(
            tenant_id=cmd.tenant_id,
            content_ref=document.storage_ref,
        )
        document_entries.append((document, content))

    # 6. Fetch the audit timeline. ``list_for_case`` already
    # enforces tenant isolation; the use case redacts every
    # payload and preserves the chain anchors.
    audit_rows = await audit.list_for_case(
        tenant_id=cmd.tenant_id,
        case_id=cmd.case_id,
        limit=10_000,  # bundle ships the full chain; the cursor is unused
    )
    audit_entries = [_audit_row_to_dict(r) for r in audit_rows]

    # 7. Build the manifest + ZIP.
    redacted_data = _redact_case_data(case, version.schema, can_see_sensitive=cmd.can_see_sensitive)
    case_dict = _case_to_dict(case, redacted_data)

    case_bytes = _canonical_json(case_dict).encode("utf-8")
    audit_bytes = _canonical_json(
        {
            "case_id": str(case.id),
            "tenant_id": str(cmd.tenant_id),
            "rows": audit_entries,
        }
    ).encode("utf-8")

    # Per-document metadata and content bytes. We build the
    # metadata after reading the content so the sha256/size reflect
    # the actual bytes the archive carries.
    document_metadata_bytes: dict[str, bytes] = {}
    document_content_bytes: dict[str, bytes] = {}
    document_entries_for_manifest: list[tuple[str, str, int, str, int]] = []
    for document, content in document_entries:
        meta_dict = _document_to_dict(
            document,
            content_sha256=_sha256_hex(content),
            content_size=len(content),
        )
        meta_name = f"documents/{document.id}/metadata.json"
        content_name = f"documents/{document.id}/content.bin"
        meta_bytes = _canonical_json(meta_dict).encode("utf-8")
        document_metadata_bytes[meta_name] = meta_bytes
        document_content_bytes[content_name] = content
        document_entries_for_manifest.append(
            (
                meta_name,
                _sha256_hex(meta_bytes),
                len(meta_bytes),
                content_name,
                len(content),
            )
        )

    # Build the entries list in the order they are written to the
    # archive: case, then per-document metadata+content, then audit.
    manifest_entries: list[dict[str, Any]] = [_manifest_entry("case.json", case_bytes)]
    for meta_name, meta_sha, meta_size, content_name, content_size in document_entries_for_manifest:
        manifest_entries.append({"name": meta_name, "sha256": meta_sha, "size": meta_size})
        manifest_entries.append(
            {
                "name": content_name,
                "sha256": _sha256_hex(document_content_bytes[content_name]),
                "size": content_size,
            }
        )
    manifest_entries.append(_manifest_entry("audit.json", audit_bytes))

    manifest: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": str(uuid4()),
        "case": {
            "id": str(case.id),
            "reference": case.reference.value,
        },
        "exported_at": _now_iso(clock),
        "exported_by": str(cmd.actor_id),
        "reason": cmd.reason,
        "can_see_sensitive": bool(cmd.can_see_sensitive),
        "document_count": len(document_entries),
        "audit_row_count": len(audit_entries),
        "entries": manifest_entries,
    }

    # The manifest hash is computed over the canonical JSON so a
    # downstream tool that re-canonicalizes the manifest gets the
    # same hash.
    manifest_bytes = _canonical_json(manifest).encode("utf-8")
    manifest_hash = _sha256_hex(manifest_bytes)

    # 8. Build the archive. The in-memory build is acceptable
    # because the bundle is bounded by the case's document count
    # + audit row count, both of which are tenant-scoped and
    # reasonable. A future slice can stream the archive through
    # ``zipfile.ZipFile`` if the bound ever stops being reasonable.
    archive = _build_archive(
        manifest_bytes=manifest_bytes,
        case_bytes=case_bytes,
        document_metadata_bytes=document_metadata_bytes,
        document_content_bytes=document_content_bytes,
        audit_bytes=audit_bytes,
    )

    # 9. Record the export action. The audit row is appended in
    # the caller's transaction (the use case does not commit); the
    # caller is the HTTP layer so the row + the read share one
    # session.
    now = _now(clock)
    export_payload: dict[str, Any] = {
        EXPORT_KEY_REASON: cmd.reason,
        EXPORT_KEY_CASE_ID: str(cmd.case_id),
        EXPORT_KEY_DOCUMENT_COUNT: len(document_entries),
        EXPORT_KEY_AUDIT_ROW_COUNT: len(audit_entries),
        EXPORT_KEY_MANIFEST_HASH: manifest_hash,
        EXPORT_KEY_BUNDLE: True,
    }
    cid = cmd.correlation_id or _payload_hash(export_payload)
    if dedupe is not None and clock is not None:
        await record_event(
            RecordAuditCommand(
                tenant_id=cmd.tenant_id,
                actor_id=cmd.actor_id,
                actor_type="user",
                event_type=AUDIT_EVIDENCE_BUNDLE_EXPORTED,
                subject_type="case",
                subject_id=cmd.case_id,
                case_id=cmd.case_id,
                correlation_id=cid,
                causation_id=None,
                payload=export_payload,
                occurred_at=now,
            ),
            audit=audit,
            dedupe=dedupe,
        )

    entries = _build_entry_list(
        manifest_bytes=manifest_bytes,
        case_bytes=case_bytes,
        document_metadata_bytes=document_metadata_bytes,
        document_content_bytes=document_content_bytes,
        audit_bytes=audit_bytes,
    )

    return EvidenceBundle(
        entries=tuple(entries),
        manifest_hash=manifest_hash,
        archive=archive,
        document_count=len(document_entries),
        audit_row_count=len(audit_entries),
    )


# Public alias for the HTTP layer and tests.
build_evidence_bundle = handle


__all__ = [
    "Action",
    "AuditEventStore",
    "CaseRepository",
    "CaseTypeVersionRepository",
    "DocumentStore",
    "NotFound",
    "RecordAuditCommand",
    "Resource",
    "_payload_hash",
    "authorize",
    "build_evidence_bundle",
    "handle",
    "record_event",
    "uuid4",
]
