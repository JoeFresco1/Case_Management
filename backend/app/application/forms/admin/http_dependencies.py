"""Dependency contracts for the admin form-binding HTTP boundary (P07 / FM-036).

The admin form-binding router needs the form-binding repository port plus
the audit store and role repository bound to one request transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.form import FormBinding
from app.domain.ports import (
    AuditEventStore,
    RoleRepository,
)


class FormBindingRepository(Protocol):
    """Read + write seam for the admin form-binding port bundle."""

    async def get_by_id(self, tenant_id: UUID, binding_id: UUID) -> FormBinding | None: ...
    async def list_for_form_version(
        self,
        tenant_id: UUID,
        form_version_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FormBinding]: ...
    async def add(self, binding: FormBinding) -> None: ...
    async def mark_published(
        self,
        tenant_id: UUID,
        binding_id: UUID,
        *,
        published_at: datetime,
        published_by: UUID,
    ) -> None: ...
    async def mark_retired(
        self,
        tenant_id: UUID,
        binding_id: UUID,
        *,
        retired_at: datetime,
        retired_by: UUID,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminFormBindingsHttpRepositories:
    """Ports required by the admin form-binding endpoints for one session."""

    form_binding: FormBindingRepository
    role: RoleRepository
    audit: AuditEventStore


class AdminFormBindingsHttpDependencyProvider(Protocol):
    """Create a request-scoped admin form-binding port bundle from a session."""

    def for_session(self, session: object) -> AdminFormBindingsHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminFormBindingsHttpDependencyProvider",
    "AdminFormBindingsHttpRepositories",
    "FormBindingRepository",
]
