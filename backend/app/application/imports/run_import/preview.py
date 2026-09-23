"""Dry-run preview use case: validate rows and report a reconciliation without writing (P14-US2)."""

from __future__ import annotations

from app.application.authorization import Action, Resource, authorize
from app.application.imports.http_dependencies import (
    ImportMappingRepository,
)
from app.application.imports.run_import.commands import PreviewImportCommand
from app.application.imports.run_import.helpers import (
    _now_clock,
    _reconciliation_from,
    _user_context,
)
from app.application.imports.run_import.policies import (
    _apply_source_id_policy,
    _resolve_mapping,
)
from app.application.imports.run_import.rows import _evaluate_row
from app.domain.errors import NotFound
from app.domain.import_job import ImportJob
from app.domain.ports import (
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
)

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def preview(
    cmd: PreviewImportCommand,
    *,
    case_type_repo: CaseTypeRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    import_mapping_repo: ImportMappingRepository | None = None,
    case_repo: CaseRepository | None = None,
) -> ImportJob:
    """Dry-run an import package without writing any case.

    Validation per row:

    1. The mapped case type exists and is published.
    2. Every required field is present in the row.
    3. The priority value (if present) is one of the allowed values.
    4. The enum values (if present) match their declared ``enum_values``.
    5. If the mapping declares a document, the content column exists and
       is a valid base64 string; the sha256 column is a valid lowercase
       hex digest; the size column is an integer under the case-type
       cap.
    6. If the mapping declares ``source_id_field``, repeat occurrences
       of one source-record id within the package are marked
       ``REJECTED`` / ``duplicate_source_id``; when ``case_repo`` is
       supplied, ids the tenant already owns are reported the same way.

    On any failure the row is marked ``REJECTED`` with a stable error
    code and a non-sensitive message. The remaining rows are still
    evaluated so the reconciliation is complete.

    The preview never writes: it stages no case, audit, or outbox row.
    Passing ``case_repo`` only authorises *reads* (already-imported
    duplicate detection); the diff between "preview with repo" and
    "preview without repo" is report-only.
    """
    # 1. Authorization.
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="import_job"),
    )

    # 2. Resolve the mapping: either from the registry (mapping_id) or
    # inline (mapping). Only published mappings may back new jobs.
    mapping = await _resolve_mapping(cmd, import_mapping_repo)

    # 3. Resolve the target case-type version. The mapping pins the key
    # so the operator cannot accidentally target a different type than
    # the mapping was authored for.
    case_type = await case_type_repo.get_by_key(cmd.tenant_id, mapping.case_type_key)
    if case_type is None:
        raise NotFound(f"case type {mapping.case_type_key!r} not found")
    if case_type.current_version_id is None:
        raise NotFound(f"case type {mapping.case_type_key!r} has no published version")
    version = await case_type_version_repo.get(cmd.tenant_id, case_type.current_version_id)
    if version is None:
        raise NotFound(f"published case type version {case_type.current_version_id} not found")

    # 4. Per-row evaluation + source-record id policy (batch
    # duplicates, and already-imported ids when a read port is given).
    evaluated = tuple(_evaluate_row(row, mapping, version) for row in cmd.rows)
    results = await _apply_source_id_policy(
        evaluated,
        cmd.rows,
        mapping,
        version,
        tenant_id=cmd.tenant_id,
        case_repo=case_repo,
    )
    reconciliation = _reconciliation_from(results)

    # 5. Attach preview to a fresh job in PREVIEWED status.
    # why: ``_now_clock()`` is a clock factory (returns a stub clock for
    # the read-only preview); the aggregate needs the instant itself, so
    # resolve ``.now()`` here. Passing the clock object stored a
    # non-datetime in ``created_at`` and broke sqlite persistence
    # (``SQLite DateTime only accepts datetime`` → 503 via idempotency).
    now = _now_clock().now()
    job = ImportJob.create(
        tenant_id=cmd.tenant_id,
        source_label=cmd.source_label,
        source_format=cmd.source_format,
        mapping=mapping,
        created_by=cmd.actor_id,
        now=now,
        correlation_id=cmd.correlation_id,
    )
    return job.attach_preview(
        source_row_count=len(cmd.rows),
        results=results,
        reconciliation=reconciliation,
        now=now,
    )
