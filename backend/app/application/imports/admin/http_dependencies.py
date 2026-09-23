"""Dependency contracts for the admin import-mapping HTTP boundary (FM-026).

The admin import-mapping router needs the mapping registry port plus
the audit store and role repository bound to one request transaction.
This module describes that bundle without importing FastAPI, SQLAlchemy,
or any infrastructure adapter. The composition root supplies the
concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.import_mapping import ImportMapping
from app.domain.ports import (
    AuditEventStore,
    RoleRepository,
)


class ImportMappingRepository(Protocol):
    """Read + write seam for the admin import-mapping port bundle."""

    async def get(self, tenant_id: UUID, mapping_id: UUID) -> ImportMapping | None: ...
    async def list_for_key(self, tenant_id: UUID, key: str) -> list[ImportMapping]: ...
    async def list_for_tenant(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[ImportMapping]: ...
    async def add(self, mapping: ImportMapping) -> None: ...
    async def mark_published(
        self,
        tenant_id: UUID,
        mapping_id: UUID,
        *,
        published_at: datetime,
        published_by: UUID,
    ) -> None: ...
    async def mark_retired(
        self,
        tenant_id: UUID,
        mapping_id: UUID,
        *,
        retired_at: datetime,
        retired_by: UUID,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminImportMappingsHttpRepositories:
    """Ports required by the admin import-mapping endpoints for one session."""

    import_mapping: ImportMappingRepository
    audit: AuditEventStore
    role: RoleRepository


class AdminImportMappingsHttpDependencyProvider(Protocol):
    """Create a request-scoped admin import-mapping port bundle from a session."""

    def for_session(self, session: object) -> AdminImportMappingsHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminImportMappingsHttpDependencyProvider",
    "AdminImportMappingsHttpRepositories",
    "ImportMappingRepository",
]
