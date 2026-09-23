"""Import pipeline input/output commands (DTOs) for preview and commit (P14-US2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.domain.import_job import ImportJob, MappingVersion

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportRow:
    """A single source row.

    Fields are arbitrary strings; the mapping converts them into the
    target ``case.data`` payload. ``index`` is the operator-facing
    ordinal (1-based). ``raw`` preserves the row for the operator UI.
    """

    index: int
    values: dict[str, str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreviewImportCommand:
    """The input to the dry-run preview phase.

    Either ``mapping`` or ``mapping_id`` must be supplied. When
    ``mapping_id`` is given, the application layer resolves the
    published mapping snapshot from the registry (FM-026); when
    ``mapping`` is given directly, it is used as-is (back-compat with
    inline payloads).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    source_label: str
    source_format: str
    mapping: MappingVersion | None = None
    mapping_id: UUID | None = None
    rows: tuple[ImportRow, ...] = ()
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class CommitImportCommand:
    """The input to the commit phase.

    ``rows`` MUST match the rows used to produce the preview — the
    commit is deterministic against the preview's reconciliation. The
    application layer re-validates and records a fresh reconciliation
    on the resulting committed job.

    Either ``mapping`` or ``mapping_id`` must be supplied (same
    resolution contract as :class:`PreviewImportCommand`).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    source_label: str
    source_format: str
    mapping: MappingVersion | None = None
    mapping_id: UUID | None = None
    rows: tuple[ImportRow, ...] = ()
    correlation_id: str = ""


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewReport:
    """The result of a dry-run preview.

    ``job`` is the :class:`ImportJob` with ``status=PREVIEWED`` carrying
    the per-row results. The caller (HTTP layer) persists the job.
    """

    job: ImportJob


@dataclass(frozen=True, slots=True)
class CommitReport:
    """The result of a successful commit."""

    job: ImportJob
    created_case_ids: tuple[UUID, ...]
    rejected_count: int
