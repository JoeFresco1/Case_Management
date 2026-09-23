"""Mapping resolution and source-record id duplicate policy for the import pipeline (FM-026)."""

from __future__ import annotations

from uuid import UUID

from app.application.imports.http_dependencies import (
    ImportMappingRepository,
)
from app.application.imports.run_import.commands import (
    CommitImportCommand,
    ImportRow,
    PreviewImportCommand,
)
from app.application.imports.source_ids import (
    attach_source_ids,
    index_existing_cases,
    overlay_batch_duplicates,
    overlay_known_sources,
    source_id_for_row,
    source_id_target_key,
    validate_source_id_binding,
)
from app.domain.case_type import CaseTypeVersion
from app.domain.errors import NotFound, ValidationFailed
from app.domain.import_job import (
    DocumentMapping,
    FieldMapping,
    ImportRowResult,
    MappingVersion,
)
from app.domain.ports import CaseRepository


async def _resolve_mapping(
    cmd: PreviewImportCommand | CommitImportCommand,
    repo: ImportMappingRepository | None,
) -> MappingVersion:
    """Resolve the effective ``MappingVersion`` from ``cmd``.

    When ``mapping_id`` is set, the mapping is fetched from the registry;
    only ``published`` mappings are eligible — a draft or retired mapping
    raises :class:`ValidationFailed` so the operator cannot accidentally
    back a job with an unpublished or retired mapping. When ``mapping``
    is set inline, it is used directly (back-compat with inline
    payloads). Exactly one of the two must be supplied.
    """
    if cmd.mapping_id is not None and cmd.mapping is not None:
        raise ValidationFailed(
            "specify either 'mapping_id' or inline 'mapping', not both",
            field="mapping_id",
        )
    if cmd.mapping_id is not None:
        if repo is None:
            raise ValidationFailed(
                "mapping_id cannot be resolved without an ImportMappingRepository",
                field="mapping_id",
            )
        stored = await repo.get(cmd.tenant_id, cmd.mapping_id)
        if stored is None:
            raise NotFound(f"import mapping {cmd.mapping_id} not found")
        if stored.status.value != "published":
            raise ValidationFailed(
                f"import mapping {cmd.mapping_id} is "
                f"{stored.status.value}; only published mappings may back jobs",
                field="mapping_id",
            )
        return MappingVersion(
            version=stored.version_number,
            case_type_key=stored.mapping_json.get("case_type_key", ""),
            fields=tuple(
                FieldMapping(
                    source=f["source"],
                    target=f["target"],
                )
                for f in stored.mapping_json.get("fields", [])
            ),
            title_field=stored.mapping_json.get("title_field"),
            description_field=stored.mapping_json.get("description_field"),
            priority_field=stored.mapping_json.get("priority_field"),
            source_id_field=stored.mapping_json.get("source_id_field"),
            documents=(
                DocumentMapping(
                    file_name_field=stored.mapping_json["documents"]["file_name_field"],
                    content_field=stored.mapping_json["documents"]["content_field"],
                    content_type_field=stored.mapping_json["documents"]["content_type_field"],
                    classification_field=stored.mapping_json["documents"]["classification_field"],
                    sha256_field=stored.mapping_json["documents"]["sha256_field"],
                )
                if stored.mapping_json.get("documents")
                else None
            ),
        )
    if cmd.mapping is None:
        raise ValidationFailed(
            "either 'mapping_id' or inline 'mapping' is required",
            field="mapping_id",
        )
    return cmd.mapping


async def _apply_source_id_policy(
    evaluated: tuple[ImportRowResult, ...],
    rows: tuple[ImportRow, ...],
    mapping: MappingVersion,
    version: CaseTypeVersion,
    *,
    tenant_id: UUID,
    case_repo: CaseRepository | None,
) -> tuple[ImportRowResult, ...]:
    """Attach source-record ids and downgrade duplicates.

    No-op when the mapping declares no ``source_id_field``. Otherwise
    the binding is validated fail-closed (the id must round-trip into
    ``case.data`` or re-submissions could never be recognised), batch
    repeats are downgraded, and — when ``case_repo`` is supplied —
    ids the tenant already owns are downgraded with the pre-existing
    ``case_id`` attached. Pure reads only; never writes.
    """
    if mapping.source_id_field is None:
        return evaluated
    validate_source_id_binding(mapping, version)
    source_ids = tuple(source_id_for_row(mapping, row.values) for row in rows)
    results = attach_source_ids(evaluated, source_ids)
    results = overlay_batch_duplicates(results, source_ids)
    if case_repo is None:
        return results
    target = source_id_target_key(mapping)
    if target is None:  # pragma: no cover — validated above, defensive only
        return results
    # Why a list-scan: the first slice has no dedicated source-id index
    # (no migration); volumes are synthetic and small. A later slice
    # that needs enterprise volumes adds a ``find_by_source_id`` port
    # method backed by an indexed column instead of widening this scan.
    owned = await case_repo.list(tenant_id)
    known = index_existing_cases(owned, target_key=target)
    return overlay_known_sources(results, known)
