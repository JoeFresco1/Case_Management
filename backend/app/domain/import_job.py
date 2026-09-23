"""ImportJob + MappingVersion aggregates (P14-US2, US3, P04-US3).

Pillar P14 owns the platform's edge to the outside world. ImportJob is the
bounded ingestion from an approved source (a connector enabled by an
authorized operator). MappingVersion is the declarative field map from a
source row to a target ``case.data`` payload.

Why this lives in the domain layer: the import pipeline is the canonical
capture channel for bulk data. It must follow the same shape as every
other mutation in the platform — authorized, audited, idempotent, and
recoverable. The aggregate carries:

- a stable id (``ImportJob`` is the unit the operator talks to),
- the versioned ``MappingVersion`` it pinned (so a re-run of an old
  package against a new mapping does not silently change semantics),
- lifecycle status (so dry-run never produces committed cases and a
  committed run never silently re-runs),
- a per-row reconciliation report (so failures are quarantined and the
  outcome is auditable, never silently lost).

# INTEGRATION_SEAM: external imports

The connector that actually produces the input bytes (CSV / JSON /
JSONL) is a P14 adapter. The first slice ships with synthetic fixtures
only (AGENTS.md §18 — no PHI, no enterprise data) and no live connector.
The pipeline shape — dry-run preview, then commit — is the contract the
future connector adapters will satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Lifecycle (P14-US2)
# ---------------------------------------------------------------------------


class ImportJobStatus(str, Enum):  # noqa: UP042 - intentional str+Enum for JSON
    """The job lifecycle.

    - ``PENDING`` — accepted but not yet processed.
    - ``PREVIEWED`` — dry-run completed; the report is attached.
    - ``COMMITTING`` — a commit is in flight.
    - ``COMMITTED`` — at least one record was accepted; the reconciliation
      report records what was rejected.
    - ``FAILED`` — the pipeline aborted before any record was committed.

    Why a separate ``COMMITTED`` (not just reusing ``PREVIEWED``): a
    committed job is a *terminal* state for the same logical import;
    further changes only happen by a new job. The terminal-vs-non-terminal
    split drives the operator UI ("is this safe to re-run?").
    """

    PENDING = "pending"
    PREVIEWED = "previewed"
    COMMITTING = "committing"
    COMMITTED = "committed"
    FAILED = "failed"


_TERMINAL_IMPORT_STATUSES: frozenset[ImportJobStatus] = frozenset(
    {ImportJobStatus.COMMITTED, ImportJobStatus.FAILED}
)


def is_terminal_import_status(status: ImportJobStatus) -> bool:
    """Return True iff ``status`` is terminal for an ``ImportJob``.

    Exposed as a module helper so the application layer, the
    reconciliation report, and tests share one definition of "done".
    """
    return status in _TERMINAL_IMPORT_STATUSES


# ---------------------------------------------------------------------------
# Per-row outcomes (P14-US4 / P14-N3)
# ---------------------------------------------------------------------------


class ImportRowOutcome(str, Enum):  # noqa: UP042
    """What happened to a single source row.

    Outcomes are mutually exclusive. ``REJECTED`` is a per-row quarantine;
    a rejected row never produces a case. ``ACCEPTED`` produced a case
    (and optionally an attached document) during the commit. ``SKIPPED``
    is reserved for explicit operator policy (not exercised in the
    first slice).
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# MappingVersion (P14-US3)
# ---------------------------------------------------------------------------


# Reserved top-level keys in a MappingVersion document. The mapping is a
# declarative JSON blob — no code — so the application layer can validate
# the shape before any row is touched.
MAPPING_KEY_VERSION: Final[str] = "version"
MAPPING_KEY_CASE_TYPE_KEY: Final[str] = "case_type_key"
MAPPING_KEY_FIELDS: Final[str] = "fields"
MAPPING_KEY_TITLE_FIELD: Final[str] = "title_field"
MAPPING_KEY_DESCRIPTION_FIELD: Final[str] = "description_field"
MAPPING_KEY_PRIORITY_FIELD: Final[str] = "priority_field"
MAPPING_KEY_SOURCE_ID_FIELD: Final[str] = "source_id_field"
MAPPING_KEY_DOCUMENTS: Final[str] = "documents"
MAPPING_KEY_DOCUMENT_FILE_FIELD: Final[str] = "file_name_field"
MAPPING_KEY_DOCUMENT_CONTENT_FIELD: Final[str] = "content_field"
MAPPING_KEY_DOCUMENT_CONTENT_TYPE_FIELD: Final[str] = "content_type_field"
MAPPING_KEY_DOCUMENT_CLASSIFICATION_FIELD: Final[str] = "classification_field"
MAPPING_KEY_DOCUMENT_SHA_FIELD: Final[str] = "sha256_field"

# Reserved field-translation keys.
MAPPING_FIELD_SOURCE: Final[str] = "source"
MAPPING_FIELD_TARGET: Final[str] = "target"


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """A single source -> target field translation.

    ``source`` is the column name in the input row; ``target`` is the
    case-type field key it maps to. Both are required. An empty target
    means the value is captured for traceability but not written to
    ``case.data`` (e.g. a ``source_record_id`` that we record on the
    audit event).
    """

    source: str
    target: str

    def __post_init__(self) -> None:
        if not self.source:
            raise InvariantViolation("field mapping requires a non-empty source")
        if not self.target:
            raise InvariantViolation("field mapping requires a non-empty target")


@dataclass(frozen=True, slots=True)
class DocumentMapping:
    """How to extract a document blob from a single input row.

    Documents are optional in a mapping. The fields name the columns in
    the input row that carry the file name, base64-encoded content,
    content-type, classification, and sha256. Missing columns raise
    ValidationFailed at commit time, never silently drop a record.
    """

    file_name_field: str
    content_field: str
    content_type_field: str
    classification_field: str
    sha256_field: str

    def __post_init__(self) -> None:
        for attr in (
            "file_name_field",
            "content_field",
            "content_type_field",
            "classification_field",
            "sha256_field",
        ):
            if not getattr(self, attr):
                raise InvariantViolation(f"document mapping requires a non-empty {attr}")


@dataclass(frozen=True, slots=True)
class MappingVersion:
    """A versioned, declarative source-to-target mapping.

    Why versioned: an operator must be able to re-run an old import
    package against the mapping that was active when the package was
    produced (Constitution XI — incremental migration). The version
    number is part of the identity.

    ``case_type_key`` pins the target ``CaseTypeVersion`` (the
    *published* version, resolved at run time by the application layer).
    The field map is a list of ``FieldMapping``s — order is preserved so
    operators see deterministic output, but the application layer uses
    a dict-style lookup keyed on ``source``.

    Why a frozen dataclass: the mapping is the contract; mutating it in
    place after a job was committed would silently change the meaning of
    the historical reconciliation. Publishing a new version is the only
    way to change a mapping.
    """

    version: int
    case_type_key: str
    fields: tuple[FieldMapping, ...]
    title_field: str | None = None
    description_field: str | None = None
    priority_field: str | None = None
    documents: DocumentMapping | None = None
    source_id_field: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvariantViolation("mapping version must be >= 1")
        if not self.case_type_key:
            raise InvariantViolation("mapping requires a non-empty case_type_key")
        if not self.fields:
            raise InvariantViolation("mapping requires at least one field")
        # Source names must be unique inside a mapping; duplicates would
        # silently shadow a column.
        sources = [f.source for f in self.fields]
        if len(sources) != len(set(sources)):
            duplicates = sorted({s for s in sources if sources.count(s) > 1})
            raise InvariantViolation(f"mapping has duplicate source fields: {duplicates}")
        # If a title/description/priority/source-id pointer names a
        # column, that column must appear in the field map.
        field_set = set(sources)
        for attr in ("title_field", "description_field", "priority_field", "source_id_field"):
            value = getattr(self, attr)
            if value and value not in field_set:
                raise InvariantViolation(f"{attr}={value!r} is not in the field map")

    def field_for(self, source: str) -> FieldMapping | None:
        """Return the mapping entry for ``source`` or None."""
        for f in self.fields:
            if f.source == source:
                return f
        return None


# ---------------------------------------------------------------------------
# ImportJob aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportRowResult:
    """The outcome for a single source row.

    Captured for the reconciliation report. ``row_index`` is the source
    row's ordinal (1-based, matching operator intuition). ``source_id``
    is the optional source-system identifier; the first slice uses an
    integer offset for it. ``error_code`` and ``error_message`` are
    populated only when ``outcome`` is ``REJECTED``; they must never
    contain raw payload data (Constitution V — redaction).
    """

    row_index: int
    outcome: ImportRowOutcome
    case_id: UUID | None = None
    source_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ImportReconciliation:
    """The reconciliation summary attached to an ``ImportJob``.

    The counts come from the per-row results; ``committed_count`` is the
    number of accepted rows, ``rejected_count`` is the number of
    quarantined rows. The combined total must equal the source row
    count (skipped rows would subtract from both).
    """

    committed_count: int
    rejected_count: int
    skipped_count: int = 0

    def __post_init__(self) -> None:
        if self.committed_count < 0:
            raise InvariantViolation("committed_count cannot be negative")
        if self.rejected_count < 0:
            raise InvariantViolation("rejected_count cannot be negative")
        if self.skipped_count < 0:
            raise InvariantViolation("skipped_count cannot be negative")


@dataclass(frozen=True, slots=True)
class ImportJob:
    """A bounded ingestion from an approved source.

    The aggregate carries the source row count, the pinned mapping, the
    reconciliation, and the per-row results. The rows are intentionally
    limited to a small batch (the first slice keeps the whole report in
    memory for the operator's preview; large imports move to streaming
    pagination in a later slice).
    """

    id: UUID
    tenant_id: UUID
    source_label: str
    source_format: str
    source_row_count: int
    mapping: MappingVersion
    status: ImportJobStatus
    created_at: datetime
    created_by: UUID
    correlation_id: str
    results: tuple[ImportRowResult, ...] = field(default_factory=tuple)
    reconciliation: ImportReconciliation | None = None
    committed_at: datetime | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.source_label:
            raise InvariantViolation("import job requires a source_label")
        if not self.source_format:
            raise InvariantViolation("import job requires a source_format")
        if self.source_row_count < 0:
            raise InvariantViolation("source_row_count cannot be negative")
        # Mapping source-format compatibility.
        _validate_source_format(self.source_format)

    # ----- factories --------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        source_label: str,
        source_format: str,
        mapping: MappingVersion,
        created_by: UUID,
        now: datetime,
        correlation_id: str,
    ) -> ImportJob:
        """Create a new ``ImportJob`` in ``PENDING`` status.

        The caller is expected to follow up with ``attach_preview`` or
        ``mark_committed`` once the dry-run / commit use case has run.
        """
        return ImportJob(
            id=uuid4(),
            tenant_id=tenant_id,
            source_label=source_label,
            source_format=source_format,
            source_row_count=0,
            mapping=mapping,
            status=ImportJobStatus.PENDING,
            created_at=now,
            created_by=created_by,
            correlation_id=correlation_id,
        )

    # ----- transitions ------------------------------------------------------

    def attach_preview(
        self,
        *,
        source_row_count: int,
        results: tuple[ImportRowResult, ...],
        reconciliation: ImportReconciliation,
        now: datetime,
    ) -> ImportJob:
        """Mark the job ``PREVIEWED`` with a dry-run report.

        The dry-run is non-mutating: no cases are created. A previously
        ``COMMITTED`` job cannot be re-previewed — operators must create
        a new job.
        """
        if self.status is not ImportJobStatus.PENDING:
            raise InvariantViolation(f"cannot preview a {self.status.value} import job")
        if source_row_count < 0:
            raise InvariantViolation("source_row_count cannot be negative")
        # The reconciliation counts must reconcile against the per-row
        # results; we assert here so a malformed report cannot be
        # attached to a job.
        _assert_reconciliation_matches(results, reconciliation)
        return _replace(
            self,
            status=ImportJobStatus.PREVIEWED,
            source_row_count=source_row_count,
            results=results,
            reconciliation=reconciliation,
        )

    def mark_committing(self) -> ImportJob:
        """Transition ``PREVIEWED`` -> ``COMMITTING`` at the start of a commit.

        Reserved for the application layer's commit use case. The
        transition is monotonic; once committing, the only legal
        terminal transition is ``mark_committed`` or ``mark_failed``.
        """
        if self.status is not ImportJobStatus.PREVIEWED:
            raise InvariantViolation(f"cannot start committing from {self.status.value}")
        return _replace(self, status=ImportJobStatus.COMMITTING)

    def mark_committed(
        self,
        *,
        results: tuple[ImportRowResult, ...],
        reconciliation: ImportReconciliation,
        at: datetime,
    ) -> ImportJob:
        """Mark the job ``COMMITTED`` with the final reconciliation.

        ``results`` and ``reconciliation`` must match the dry-run
        preview's accepted records (the commit does not re-introduce
        rejected records).
        """
        if self.status is not ImportJobStatus.COMMITTING:
            raise InvariantViolation(f"cannot commit from {self.status.value}")
        _assert_reconciliation_matches(results, reconciliation)
        return _replace(
            self,
            status=ImportJobStatus.COMMITTED,
            results=results,
            reconciliation=reconciliation,
            committed_at=at,
        )

    def mark_failed(self, *, reason: str, at: datetime) -> ImportJob:
        """Mark the job ``FAILED``.

        ``FAILED`` is terminal; no further transitions are allowed. The
        reason is a short, non-sensitive string (no payload data).
        """
        if self.status is ImportJobStatus.COMMITTED:
            raise InvariantViolation("a committed job cannot fail")
        if not reason:
            raise ValidationFailed("failure_reason is required")
        return _replace(
            self,
            status=ImportJobStatus.FAILED,
            failure_reason=reason,
            committed_at=at,
        )


# ---------------------------------------------------------------------------
# ImportPackage (spec name) — the durable bulk-ingestion unit
# ---------------------------------------------------------------------------


# The 002-leftovers spec (§5) names the new entity ``ImportPackage``;
# the code's durable unit is :class:`ImportJob` (lifecycle + pinned
# mapping + reconciliation). They are the same aggregate — this alias
# keeps spec traceability without a second table or a parallel type.
# why: defined after ``ImportJob`` so module import never hits a
# forward-reference NameError; the alias is identity, not a subclass.
ImportPackage = ImportJob


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SUPPORTED_FORMATS: frozenset[str] = frozenset({"csv", "json", "jsonl"})


def _validate_source_format(value: str) -> None:
    if value not in _SUPPORTED_FORMATS:
        raise ValidationFailed(
            f"unsupported source_format: {value!r} (expected one of {sorted(_SUPPORTED_FORMATS)})",
            field="source_format",
        )


def _assert_reconciliation_matches(
    results: tuple[ImportRowResult, ...],
    reconciliation: ImportReconciliation,
) -> None:
    """Raise if the reconciliation counts do not match the per-row results."""
    committed = sum(1 for r in results if r.outcome is ImportRowOutcome.ACCEPTED)
    rejected = sum(1 for r in results if r.outcome is ImportRowOutcome.REJECTED)
    skipped = sum(1 for r in results if r.outcome is ImportRowOutcome.SKIPPED)
    if (
        committed != reconciliation.committed_count
        or rejected != reconciliation.rejected_count
        or skipped != reconciliation.skipped_count
    ):
        raise InvariantViolation(
            "reconciliation counts do not match per-row results: "
            f"committed={committed} rejected={rejected} skipped={skipped} "
            f"vs reconciliation={reconciliation}"
        )


def _replace(job: ImportJob, **changes: Any) -> ImportJob:
    """Internal helper to keep the aggregate immutable under transitions."""
    return replace(job, **changes)


__all__ = [
    "DocumentMapping",
    "FieldMapping",
    "ImportJob",
    "ImportJobStatus",
    "ImportPackage",
    "ImportReconciliation",
    "ImportRowOutcome",
    "ImportRowResult",
    "MappingVersion",
    "is_terminal_import_status",
]
