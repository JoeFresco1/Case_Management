"""Import pipeline use case (P14-US2, US3, US4 / P04-US3 / FM-026).

The use case is the canonical entry point for bulk ingestion from an
approved source. The pipeline has two terminal phases that share one
piece of code:

- **dry-run preview** — validate every row against the mapping and the
  published ``CaseTypeVersion`` schema, build a per-row reconciliation
  report, and **never** write any case or document. The output is an
  :class:`ImportJob` with ``status=PREVIEWED`` attached to the
  reconciliation. The operator reviews the report, fixes any source
  errors, and commits the same job.

- **commit** — re-run validation on the same inputs and create one case
  per accepted row, plus an attached document if the mapping declares
  one. Each case creation is atomic with audit + outbox
  (``CaseCreated`` / ``DocumentAdded`` events). Rejected rows are
  recorded as ``ImportRowOutcome.REJECTED`` with a non-sensitive reason
  code; the rejected rows are quarantined, never silently dropped.

# INTEGRATION_SEAM: external imports

The actual byte source (CSV / JSON / JSONL) is the responsibility of the
P14 connector adapter. The first slice takes synthetic inline rows; the
adapter that reads from an enterprise system plugs in behind the
``ImportRowsSource`` callable passed to the use case. Production wires
the CSV / JSON / JSONL reader via ``app.infrastructure.imports``.

# Multi-tenant posture

Every write carries the actor's ``tenant_id``. Cross-tenant requests
are denied at the policy layer (``Action.ADMIN_TENANT_CONFIG`` is
required, mirroring the outbox / case-type admin endpoints). The
created cases inherit the actor's tenant — the platform is the system of
record for migrated cases (P14-Q4).
"""

from __future__ import annotations

from app.application.imports.run_import.commands import (
    CommitImportCommand,
    CommitReport,
    ImportRow,
    PreviewImportCommand,
    PreviewReport,
)
from app.application.imports.run_import.commit import commit
from app.application.imports.run_import.helpers import (
    DOCUMENT_ADDED as DOCUMENT_ADDED,
)
from app.application.imports.run_import.helpers import (
    ERROR_DOCUMENT_CONTENT_INVALID,
    ERROR_DOCUMENT_CONTENT_MISSING,
    ERROR_DOCUMENT_HASH_INVALID,
    ERROR_DOCUMENT_SIZE_EXCEEDED,
    ERROR_INVALID_ENUM,
    ERROR_INVALID_PRIORITY,
    ERROR_MISSING_REQUIRED_FIELD,
    ERROR_MISSING_TITLE,
    ERROR_UNKNOWN_FIELD,
)
from app.application.imports.run_import.helpers import (
    ERROR_DOCUMENT_STORAGE_FAILED as ERROR_DOCUMENT_STORAGE_FAILED,
)
from app.application.imports.run_import.helpers import (
    IMPORT_COMMITTED as IMPORT_COMMITTED,
)
from app.application.imports.run_import.helpers import (
    IMPORT_PREVIEWED as IMPORT_PREVIEWED,
)
from app.application.imports.run_import.helpers import (
    IMPORT_ROW_REJECTED as IMPORT_ROW_REJECTED,
)
from app.application.imports.run_import.helpers import (
    SUBJECT_TYPE_CASE as SUBJECT_TYPE_CASE,
)
from app.application.imports.run_import.helpers import (
    SUBJECT_TYPE_DOCUMENT as SUBJECT_TYPE_DOCUMENT,
)
from app.application.imports.run_import.helpers import (
    SUBJECT_TYPE_IMPORT_JOB as SUBJECT_TYPE_IMPORT_JOB,
)
from app.application.imports.run_import.preview import preview
from app.application.imports.source_ids import ERROR_DUPLICATE_SOURCE_ID

__all__ = [
    "CommitImportCommand",
    "CommitReport",
    "ERROR_DOCUMENT_CONTENT_INVALID",
    "ERROR_DOCUMENT_CONTENT_MISSING",
    "ERROR_DOCUMENT_HASH_INVALID",
    "ERROR_DOCUMENT_SIZE_EXCEEDED",
    "ERROR_DUPLICATE_SOURCE_ID",
    "ERROR_INVALID_ENUM",
    "ERROR_INVALID_PRIORITY",
    "ERROR_MISSING_REQUIRED_FIELD",
    "ERROR_MISSING_TITLE",
    "ERROR_UNKNOWN_FIELD",
    "ImportRow",
    "PreviewImportCommand",
    "PreviewReport",
    "commit",
    "preview",
]
