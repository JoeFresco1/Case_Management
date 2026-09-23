"""Intake use-case orchestration: ``handle`` and its private helpers.

The async entry point that turns a :class:`~.commands.SubmitIntakeCommand`
into a persisted :class:`~app.domain.case.Case` plus audit, outbox, and
document rows in one transaction. The command/result dataclasses live in
:mod:`.commands`, the Protocol seams in :mod:`.ports`, and the
deterministic field stages in :mod:`.field_processing`; this module owns
only the pipeline ordering (authorize -> resolve -> enrich -> validate ->
store attachments -> persist) and the persistence writes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.compute_derived import compute_derived_fields
from app.domain.audit import CASE_CREATED
from app.domain.case import Case
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.form import (
    FormVersionStatus,
    apply_submission_preservation,
    read_only_field_keys,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

from .commands import AttachmentBlob, SubmitIntakeCommand, SubmitIntakeResult, _ActorProxy
from .field_processing import (
    _is_blank,
    apply_calculated_fields,
    apply_lookup_prepopulation,
    apply_system_fields,
    check_signature_fields,
    field_map,
    hidden_field_keys,
    route_attachment_references,
    validate_visible_fields_deterministic,
)
from .ports import (
    DOCUMENT_ADDED,
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DOCUMENT,
    FormBindingRepository,
    FormVersionLoader,
    LookupProvider,
    SignatureConnector,
    _ClockLike,
)


async def handle(  # noqa: PLR0912, PLR0915 -- intake pipeline stages by design
    cmd: SubmitIntakeCommand,
    *,
    case_repo: CaseRepository,
    case_type_repo: CaseTypeRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    form_binding_repo: FormBindingRepository,
    form_version_loader: FormVersionLoader,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    lookup: LookupProvider | None = None,
    signature_connector: SignatureConnector | None = None,
) -> SubmitIntakeResult:
    """Submit a published case-bound form as a new case (T3-US1 / FM-036).

    Failure behavior:

    - Auth / binding / version / validation errors raise before any DB
      write; the caller retains ``submitted_data`` so the UI re-renders
      entered values with per-field highlights (acceptance: errors
      identify fields without losing data).
    - The case insert, document rows, audit rows, and outbox rows share
      one transaction; the caller commits once. A ``DocumentStore``
      failure (``DependencyUnavailable``) or an audit/outbox failure
      rolls everything back — no orphaned case, no silent loss.
    - The HTTP ``Idempotency-Key`` middleware makes retries safe: a
      replayed key returns the cached response without re-executing.

    Raises:
        Forbidden: actor lacks ``CASE_CREATE`` or the form belongs to a
            different tenant (fail-closed, no existence oracle).
        NotFound: binding, form version, case type, or case-type version
            missing in this tenant.
        ValidationFailed: unpublished/retired form, binding mismatch,
            deterministic field/attachment/signature failures (``field``
            names every offender).
        DependencyUnavailable: document store, lookup source, or
            signature connector unreachable (caller rolls back).
    """
    await authorize(
        Action.CASE_CREATE,
        _ActorProxy(  # type: ignore[arg-type]
            tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles
        ),
        Resource(tenant_id=cmd.tenant_id, kind="case_type", id=None),
    )

    # 1. Resolve binding + form version (published only, tenant-scoped).
    binding = await form_binding_repo.get_published_for_key(cmd.tenant_id, cmd.form_key)
    if binding is None:
        raise ValidationFailed(f"no published form binding for {cmd.form_key!r}")
    if binding.tenant_id != cmd.tenant_id:
        raise Forbidden("form belongs to a different tenant")
    form_version = await form_version_loader.get_by_id(cmd.tenant_id, binding.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {binding.form_version_id} not found")
    if form_version.status is not FormVersionStatus.PUBLISHED:
        raise ValidationFailed(
            f"form {form_version.key!r} is not published (status={form_version.status.value})"
        )
    if form_version.tenant_id != cmd.tenant_id:
        raise Forbidden("form belongs to a different tenant")

    # 2. Resolve case type lineage pinned by the binding.
    case_type_version = await case_type_version_repo.get(
        cmd.tenant_id, binding.case_type_version_id
    )
    if case_type_version is None:
        raise NotFound(f"case type version {binding.case_type_version_id} not found")
    case_type = await case_type_repo.get(cmd.tenant_id, case_type_version.case_type_id)
    if case_type is None:
        raise NotFound(f"case type {case_type_version.case_type_id} not found")

    definition = form_version.definition
    visible_sections = form_version.visible_section_keys(
        visible_data={},
        submission_data=cmd.submitted_data,
    )
    hidden_keys = hidden_field_keys(definition, visible_sections)
    read_only = read_only_field_keys(definition)

    # 3. Merge over empty stored data, dropping hidden + read-only inbound.
    # why: intake has no prior row; hidden sections contribute nothing
    # and computed keys are platform-owned (caller values never win).
    merged = apply_submission_preservation(
        stored_data={},
        submitted_data=cmd.submitted_data,
        hidden_keys=hidden_keys,
        read_only_keys=read_only,
    )

    # 4. Lookup pre-population (fills blanks deterministically).
    merged = await apply_lookup_prepopulation(
        definition, merged, tenant_id=cmd.tenant_id, lookup=lookup
    )

    # 5. Calculated fields (formula-owned values).
    merged, calculated_keys = apply_calculated_fields(definition, merged)

    # 6. Reference first (system fields may cite it), then system fields.
    now = clock.now()
    reference = await case_repo.next_reference(cmd.tenant_id, now=now, type_key=case_type.key)
    merged = apply_system_fields(
        definition,
        merged,
        system_context={
            "case.reference": reference.value,
            "case.status": "open",
            "case.created_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
            "case.created_by": str(cmd.actor_id),
            "actor.id": str(cmd.actor_id),
            "actor.display_name": "",
            "tenant.id": str(cmd.tenant_id),
        },
    )

    # 7. Signature seam (external kinds fail closed without a connector).
    await check_signature_fields(
        definition, merged, tenant_id=cmd.tenant_id, connector=signature_connector
    )

    # 7b. Inline attachment blobs: spec-check now (field-named errors
    # before any write) and stamp a pending marker so the required check
    # below sees the field as supplied. Markers are replaced with real
    # document ids after the store step; they never reach the audit row.
    fields = field_map(definition)
    for key, spec in fields.items():
        if spec.field_type != "attachment" or not _is_blank(merged.get(key)):
            continue
        blob = cmd.attachments.get(key) if isinstance(cmd.attachments, Mapping) else None
        if blob is None:
            continue  # why: blank + required is the validator's job.
        _check_blob_against_spec(key, spec, blob)
        merged[key] = {"document_id": "pending"}

    # 8. Deterministic validation (all offenders named; nothing written).
    validate_visible_fields_deterministic(
        definition=definition,
        visible_sections=visible_sections,
        merged_data=merged,
    )

    # 9. Build the case object (in memory; nothing staged yet). Title
    # falls back to the ``title`` field value so the form stays the
    # single source of truth for intake titles.
    title = cmd.title.strip() or str(merged.get("title") or "").strip()
    data_payload = {k: v for k, v in merged.items() if not k.startswith("__")}
    case = Case.create(
        tenant_id=cmd.tenant_id,
        reference=reference,
        case_type_version=case_type_version,
        title=title,
        description=cmd.description,
        data=data_payload,
        priority=cmd.priority,
        created_by=cmd.actor_id,
        now=now,
        sensitivity=case_type.sensitivity_default,
    )
    # why: case-type derived fields are the stored contract (FM-012);
    # caller/form values for derived keys are replaced, like create_case.
    case = _replace_data(case, compute_derived_fields(case.data, case_type_version.schema))

    # 10. Attachment routing: store inline blobs BEFORE staging any row.
    # A store failure raises DependencyUnavailable with nothing staged,
    # so the caller has nothing to roll back (acceptance: dependency
    # failure rolls back). Bytes orphaned by a later audit/outbox
    # failure are hash-addressed and deduplicated on retry, exactly like
    # the ``upload_document`` contract.
    stored_blobs: dict[str, tuple[Document, DocumentVersion]] = {}
    stored_ids: dict[str, str] = {}
    document_ids: dict[str, UUID] = {}
    for key, spec in fields.items():
        if spec.field_type != "attachment":
            continue
        blob = cmd.attachments.get(key) if isinstance(cmd.attachments, Mapping) else None
        if blob is None:
            continue
        sha256 = hashlib.sha256(blob.content).hexdigest()
        storage_ref = await document_store.put(
            tenant_id=cmd.tenant_id,
            content=blob.content,
            content_type=blob.content_type,
            sha256=sha256,
        )
        document = Document.create(
            tenant_id=cmd.tenant_id,
            case_id=case.id,
            file_name=blob.file_name,
            content_type=blob.content_type,
            classification=spec.attachment_classification or "Correspondence",
            sensitivity=DocumentSensitivity.INTERNAL,
            sha256=sha256,
            size=len(blob.content),
            storage_ref=storage_ref,
            uploaded_by=cmd.actor_id,
            now=now,
        )
        version_row = DocumentVersion(
            id=uuid4(),
            tenant_id=cmd.tenant_id,
            document_id=document.id,
            version_number=1,
            sha256=sha256,
            size=len(blob.content),
            storage_ref=storage_ref,
            content_type=blob.content_type,
            uploaded_by=cmd.actor_id,
            uploaded_at=now,
        )
        stored_blobs[key] = (document, version_row)
        stored_ids[key] = str(document.id)
        document_ids[key] = document.id

    # 11. Verify pre-uploaded attachment references resolve in-tenant
    # (still before any write; cross-tenant ids fail as field errors,
    # never as an existence oracle per Constitution V).
    for key, spec in fields.items():
        if spec.field_type != "attachment" or _is_blank(merged.get(key)):
            continue
        value = merged.get(key)
        if key in stored_ids or not (
            isinstance(value, Mapping) and str(value.get("document_id") or "").strip()
        ):
            continue
        if str(value.get("document_id")) == "pending":
            continue  # why: stamped above; replaced with the real id next.
        try:
            doc_id = UUID(str(value.get("document_id")))
        except ValueError as exc:
            raise ValidationFailed(
                spec.validation_message or f"attachment {key!r} needs an uploaded document",
                field=key,
            ) from exc
        existing_doc = await document_repo.get(cmd.tenant_id, doc_id)
        if existing_doc is None:
            raise ValidationFailed(
                f"attachment {key!r} references an unknown document",
                field=key,
            )
        document_ids[key] = doc_id

    # 12. Point pending markers at the stored ids, then stage case +
    # audit + outbox (+ documents) in one transaction (caller commits
    # once; an audit/outbox failure rolls the case insert back).
    routed = route_attachment_references(definition, merged, known_document_ids=stored_ids)
    case = _replace_data(case, {k: v for k, v in routed.items() if not k.startswith("__")})
    case = _replace_data(case, compute_derived_fields(case.data, case_type_version.schema))

    await case_repo.add(case)
    correlation_id = cmd.correlation_id or _new_correlation_id()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=case.id,
        case_id=case.id,
        correlation_id=correlation_id,
        causation_id=cmd.causation_id,
        payload={
            "reference": case.reference.value,
            "case_type_key": case_type.key,
            "case_type_version_id": str(case.case_type_version_id),
            "priority": case.priority,
            "title": case.title,
            "status": case.status.value,
            "form_key": form_version.key,
            "form_version_id": str(form_version.id),
            "form_version_number": form_version.version_number,
            "hidden_keys": sorted(hidden_keys),
            "calculated_keys": sorted(calculated_keys),
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_CREATED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "reference": case.reference.value,
            "case_type_key": case_type.key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "form_key": form_version.key,
            "form_version_id": str(form_version.id),
            "occurred_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
        },
    )
    for key in sorted(stored_blobs):
        document, version_row = stored_blobs[key]
        await document_repo.add_document_and_version(document=document, version=version_row)
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=DOCUMENT_ADDED,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type=SUBJECT_TYPE_DOCUMENT,
            subject_id=document.id,
            case_id=case.id,
            correlation_id=correlation_id,
            causation_id=cmd.causation_id,
            payload={
                "case_id": str(case.id),
                "document_id": str(document.id),
                "file_name": document.file_name,
                "classification": document.classification,
                "form_key": form_version.key,
                "form_field": key,
                "sha256": document.sha256,
                "size": document.size,
            },
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=DOCUMENT_ADDED,
            event_id=uuid4(),
            payload={
                "case_id": str(case.id),
                "document_id": str(document.id),
                "classification": document.classification,
                "form_key": form_version.key,
                "form_field": key,
                "occurred_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
            },
        )

    return SubmitIntakeResult(
        case=case,
        hidden_keys=hidden_keys,
        calculated_keys=calculated_keys,
        document_ids=document_ids,
    )


def _check_blob_against_spec(key: str, spec: Any, blob: AttachmentBlob) -> None:
    """Validate an inline blob against the attachment field contract."""
    allowed = tuple(spec.attachment_allowed_content_types or ())
    if allowed and blob.content_type not in allowed:
        raise ValidationFailed(
            f"attachment {key!r} content type {blob.content_type!r} is not permitted",
            field=key,
        )
    limit = spec.attachment_max_size_bytes
    if limit is not None and len(blob.content) > limit:
        raise ValidationFailed(
            f"attachment {key!r} exceeds configured max size ({limit} bytes)",
            field=key,
        )
    if not blob.file_name.strip():
        raise ValidationFailed(f"attachment {key!r} needs a file name", field=key)


def _replace_data(case: Case, data: dict[str, Any]) -> Case:
    """Return ``case`` with ``data`` replaced (frozen dataclass helper)."""
    from dataclasses import replace  # noqa: PLC0415 -- local to keep import block lean.

    return replace(case, data=data)


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "handle",
    "_check_blob_against_spec",
    "_new_correlation_id",
    "_replace_data",
]
