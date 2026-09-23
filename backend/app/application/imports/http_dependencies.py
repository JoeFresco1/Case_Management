"""Dependency contracts for the import pipeline HTTP composition boundary.

The imports router needs several ports bound to one request
transaction: the case / case-type / document repositories, the audit
and outbox writers, the role repository for the
``ADMIN_TENANT_CONFIG`` authorization check, a durable recorder for the
resulting ``ImportJob`` aggregate, and the import-mapping registry for
``mapping_id`` resolution (FM-026). This module describes that
bundle without importing FastAPI, SQLAlchemy, or any infrastructure
adapter. The composition root supplies the concrete provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.domain.import_job import ImportJob
from app.domain.import_mapping import ImportMapping
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
    RoleRepository,
)


class ImportMappingRepository(Protocol):
    """Read seam for the versioned import-mapping registry (FM-026).

    Only ``published`` mappings are eligible to back new jobs; a
    ``retired`` mapping blocks new jobs but old jobs keep their inline
    snapshot. The application layer resolves a ``mapping_id`` through
    this port so the router can accept either an inline mapping
    (back-compat) or a persisted mapping reference.
    """

    async def get(self, tenant_id: UUID, mapping_id: UUID) -> ImportMapping | None:
        """Return the mapping row for ``mapping_id`` in ``tenant_id``."""

        ...

    async def get_published_for_key(self, tenant_id: UUID, key: str) -> ImportMapping | None:
        """Return the latest published mapping for ``key``, or None."""

        ...

    async def list_published_for_key(self, tenant_id: UUID, key: str) -> Sequence[ImportMapping]:
        """List published versions for ``key``, newest first."""

        ...


class ImportJobRecorder(Protocol):
    """Durable persistence for the import-job aggregate (write + read).

    The application layer owns the ``ImportJob`` aggregate; the
    adapter translates it into the relational row (including the
    mapping snapshot and the per-row results JSON) and stages it on
    the request transaction. The caller commits once at the boundary
    so the job row shares the transaction with any audit / outbox
    rows the commit phase staged.

    Read methods return the reconstructed ``ImportJob`` aggregate so
    the operator UI can inspect a previously previewed or committed
    job without re-computing the reconciliation.
    """

    async def record(self, job: ImportJob, mapping: dict[str, Any]) -> None:
        """Stage ``job`` with its ``mapping`` snapshot for persistence.

        Args:
            job: the previewed or committed aggregate to persist.
            mapping: the verbatim mapping snapshot the job was
                validated and committed with, so a later replay uses
                the same mapping (Constitution XI).
        """

        ...

    async def get(self, tenant_id: UUID, job_id: UUID) -> ImportJob | None:
        """Return the job aggregate for ``job_id`` in ``tenant_id``."""

        ...

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ImportJob]:
        """List import jobs for ``tenant_id``, newest first."""

        ...


@dataclass(frozen=True, slots=True)
class ImportHttpRepositories:
    """Ports required by the import endpoints for one session."""

    case: CaseRepository
    case_type: CaseTypeRepository
    case_type_version: CaseTypeVersionRepository
    document: DocumentRepository
    audit: AuditEventStore
    outbox: OutboxWriter
    role: RoleRepository
    import_jobs: ImportJobRecorder
    import_mappings: ImportMappingRepository


class ImportHttpDependencyProvider(Protocol):
    """Create a request-scoped import port bundle from a persistence session."""

    def for_session(self, session: object) -> ImportHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "ImportHttpDependencyProvider",
    "ImportHttpRepositories",
    "ImportJobRecorder",
    "ImportMappingRepository",
]
