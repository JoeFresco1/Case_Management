"""Source-record id policy for the import pipeline (FM-026 / T1-US1, T1-US2).

Why this module exists: the spec requires a dry-run preview that reports
``creates / updates / rejects / duplicates`` and a commit where
"re-submitting same source-record id is idempotent (no duplicate case)".
The mapping's optional ``source_id_field`` names the source column that
carries the stable upstream identifier (e.g. ``request_id``). This module
holds the pure policy — no I/O, no repository access — so both the
dry-run preview and the commit share one definition of "duplicate":

- **batch-internal duplicates** — the same non-empty source id appears
  on two rows of one package. The first occurrence keeps its verdict;
  later ``ACCEPTED`` occurrences are downgraded to ``REJECTED`` with
  ``duplicate_source_id``. Rows already rejected for another reason keep
  their original error (root cause first, duplicate second).
- **already-imported sources** — the source id matches a case the tenant
  already owns (built by the caller via :func:`index_existing_cases`).
  ``ACCEPTED`` rows are downgraded to ``REJECTED`` with
  ``duplicate_source_id`` and the existing ``case_id`` attached for
  operator correlation. No new case is created for them, which is what
  makes re-submission idempotent.

"Updates" are intentionally out of scope for this slice: a re-submitted
source id never mutates the existing case — the row is reported as a
duplicate and the original case is untouched (Constitution XI —
incremental migration without silent overwrites). In-place updates of
previously imported cases, if ever required, belong to a later slice
with their own concurrency story.

# INTEGRATION_SEAM: external imports

A future enterprise connector may supply the source id out-of-band
(message envelope, file lineage) instead of as a row column. That
adapter maps the envelope id into the same ``source_id`` slot consumed
here; the overlay functions below do not change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from app.domain.case import Case
from app.domain.case_type import CaseTypeVersion
from app.domain.errors import ValidationFailed
from app.domain.import_job import (
    ImportRowOutcome,
    ImportRowResult,
    MappingVersion,
)

# Stable per-row error code for both duplicate buckets. Subscribers and
# the operator UI key on this string to bucket rejections.
ERROR_DUPLICATE_SOURCE_ID: Final[str] = "duplicate_source_id"


def source_id_for_row(mapping: MappingVersion, values: Mapping[str, str]) -> str | None:
    """Return the row's stable source-record id, or None.

    Args:
        mapping: The pinned mapping; ``source_id_field`` names the
            source column (None means the package carries no stable id
            and every row is treated as a fresh create).
        values: The raw source-column -> string map for one row.

    Empty / whitespace-only values are treated as absent — a row
    without an id can never collide, so it is never a duplicate.
    """
    if mapping.source_id_field is None:
        return None
    raw = values.get(mapping.source_id_field, "")
    stripped = raw.strip()
    return stripped or None


def source_id_target_key(mapping: MappingVersion) -> str | None:
    """Return the ``case.data`` key that persists the source id, or None.

    The source id reaches the stored case through the regular field map
    (``source_id_field`` -> target), so idempotency lookups compare
    ``case.data[target]`` against the row's source id. Returns None when
    the mapping declares no ``source_id_field``.
    """
    if mapping.source_id_field is None:
        return None
    field = mapping.field_for(mapping.source_id_field)
    return field.target if field is not None else None


def validate_source_id_binding(mapping: MappingVersion, version: CaseTypeVersion) -> None:
    """Fail closed when ``source_id_field`` cannot round-trip to storage.

    Raises:
        ValidationFailed: If ``source_id_field`` names a column with no
            field-map entry, or whose target is not a field the target
            case-type schema declares. Without a stored copy of the id,
            a later re-submission could not be recognised, so the job is
            refused up front rather than silently duplicating later.
    """
    if mapping.source_id_field is None:
        return
    field = mapping.field_for(mapping.source_id_field)
    if field is None:
        raise ValidationFailed(
            f"source_id_field={mapping.source_id_field!r} is not in the field map",
            field="source_id_field",
        )
    if field.target not in set(version.schema.field_keys()):
        raise ValidationFailed(
            f"source_id_field={mapping.source_id_field!r} maps to "
            f"{field.target!r}, which the case type does not declare",
            field="source_id_field",
        )


def attach_source_ids(
    results: tuple[ImportRowResult, ...],
    source_ids: tuple[str | None, ...],
) -> tuple[ImportRowResult, ...]:
    """Populate ``source_id`` on every result for operator correlation.

    Args:
        results: Per-row results from validation, in row order.
        source_ids: The extracted source id per row, in the same order.
            Must have the same length as ``results``.
    """
    if len(results) != len(source_ids):
        raise ValidationFailed("row/source-id count mismatch", field="source_id_field")
    out: list[ImportRowResult] = []
    for result, source_id in zip(results, source_ids, strict=True):
        if result.source_id == source_id:
            out.append(result)
            continue
        out.append(
            ImportRowResult(
                row_index=result.row_index,
                outcome=result.outcome,
                case_id=result.case_id,
                source_id=source_id,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        )
    return tuple(out)


def overlay_batch_duplicates(
    results: tuple[ImportRowResult, ...],
    source_ids: tuple[str | None, ...],
) -> tuple[ImportRowResult, ...]:
    """Downgrade repeat occurrences of one source id within a package.

    The first row carrying an id keeps its verdict; each later
    ``ACCEPTED`` row with the same id becomes ``REJECTED`` /
    ``duplicate_source_id``. Rows rejected for another reason keep
    their original error. Rows without an id are untouched.
    """
    if len(results) != len(source_ids):
        raise ValidationFailed("row/source-id count mismatch", field="source_id_field")
    seen: set[str] = set()
    out: list[ImportRowResult] = []
    for result, source_id in zip(results, source_ids, strict=True):
        if source_id is None:
            # A row without an id can never collide — keep its verdict.
            out.append(result)
            continue
        if source_id not in seen:
            # First occurrence keeps its verdict (valid or not).
            seen.add(source_id)
            out.append(result)
            continue
        if result.outcome is not ImportRowOutcome.ACCEPTED:
            # Already rejected for a root cause — keep that error.
            out.append(result)
            continue
        out.append(
            ImportRowResult(
                row_index=result.row_index,
                outcome=ImportRowOutcome.REJECTED,
                case_id=None,
                source_id=source_id,
                error_code=ERROR_DUPLICATE_SOURCE_ID,
                error_message=(
                    f"duplicate source-record id {source_id!r} within this import package"
                ),
            )
        )
    return tuple(out)


def index_existing_cases(
    cases: Sequence[Case],
    *,
    target_key: str,
) -> dict[str, UUID]:
    """Build a source-id -> case-id index over the tenant's cases.

    Args:
        cases: Cases already owned by the tenant (the caller scopes
            the read to ``tenant_id``; this helper never filters).
        target_key: The ``case.data`` key holding the stored source id.

    First write wins: if two cases somehow carry the same stored id,
    the index points at the first one in iteration order.
    """
    index: dict[str, UUID] = {}
    for case in cases:
        raw = case.data.get(target_key)
        if not isinstance(raw, str):
            continue
        stripped = raw.strip()
        if not stripped or stripped in index:
            continue
        index[stripped] = case.id
    return index


def reconciliation_counts(
    results: tuple[ImportRowResult, ...],
) -> tuple[int, int, int]:
    """Return ``(committed, rejected, skipped)`` totals for ``results``.

    Pure helper shared by the dry-run preview and the commit so both
    phases bucket outcomes identically across ``csv`` / ``json`` /
    ``jsonl`` packages. The ``source_format`` string is provenance only
    (validated by :class:`ImportJob`); counting never branches on it.

    Args:
        results: Per-row results in row order.

    Returns:
        A ``(committed, rejected, skipped)`` triple matching the
        :class:`ImportReconciliation` field order.
    """
    committed = sum(1 for r in results if r.outcome is ImportRowOutcome.ACCEPTED)
    rejected = sum(1 for r in results if r.outcome is ImportRowOutcome.REJECTED)
    skipped = sum(1 for r in results if r.outcome is ImportRowOutcome.SKIPPED)
    return committed, rejected, skipped


def overlay_known_sources(
    results: tuple[ImportRowResult, ...],
    known: Mapping[str, UUID],
) -> tuple[ImportRowResult, ...]:
    """Downgrade rows whose source id is already imported.

    ``ACCEPTED`` rows with a source id present in ``known`` become
    ``REJECTED`` / ``duplicate_source_id`` with the pre-existing
    ``case_id`` attached so the operator can navigate to the original
    case. Nothing is mutated — the caller skips case creation for
    these rows, which is what makes re-submission idempotent.
    """
    out: list[ImportRowResult] = []
    for result in results:
        if (
            result.outcome is not ImportRowOutcome.ACCEPTED
            or result.source_id is None
            or result.source_id not in known
        ):
            out.append(result)
            continue
        out.append(
            ImportRowResult(
                row_index=result.row_index,
                outcome=ImportRowOutcome.REJECTED,
                case_id=known[result.source_id],
                source_id=result.source_id,
                error_code=ERROR_DUPLICATE_SOURCE_ID,
                error_message=(
                    f"source-record id {result.source_id!r} was already imported "
                    f"as case {known[result.source_id]}"
                ),
            )
        )
    return tuple(out)


__all__ = [
    "ERROR_DUPLICATE_SOURCE_ID",
    "attach_source_ids",
    "index_existing_cases",
    "overlay_batch_duplicates",
    "overlay_known_sources",
    "reconciliation_counts",
    "source_id_for_row",
    "source_id_target_key",
    "validate_source_id_binding",
]
