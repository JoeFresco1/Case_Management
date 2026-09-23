"""Dependency contracts for the admin CaseType HTTP composition boundary.

The admin CaseType router needs several ports bound to one request
transaction. This module describes that bundle without importing FastAPI,
SQLAlchemy, or any infrastructure adapter. The composition root supplies the
concrete provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.configuration import governance
from app.domain.ports import (
    AuditEventStore,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    RoleRepository,
)


@dataclass(frozen=True, slots=True)
class AdminCaseTypesHttpRepositories:
    """Ports required by the admin CaseType endpoints for one session."""

    case_type: CaseTypeRepository
    case_type_version: CaseTypeVersionRepository
    audit: AuditEventStore
    role: RoleRepository


class AdminCaseTypesHttpDependencyProvider(Protocol):
    """Create a request-scoped admin CaseType port bundle from a session."""

    def for_session(self, session: object) -> AdminCaseTypesHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


class GovernancePublicationRepository(Protocol):
    """Persistence contract used by the governance HTTP endpoints."""

    async def add(
        self,
        *,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        version_id: UUID,
        action: str,
        actor_id: UUID,
        change_summary: str,
        occurred_at: datetime,
    ) -> UUID:
        """Append one tenant-scoped publication record."""

        ...

    async def list_for_artifact(
        self,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[governance.PublicationRecord]:
        """List one artifact's publication history for a tenant."""

        ...

    async def list_tenant_history(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        artifact_type: str | None = None,
    ) -> Sequence[governance.PublicationRecord]:
        """List publication history for one tenant."""

        ...


class GovernancePolicy(Protocol):
    """Contract for the tenant's second-administrator approval policy."""

    async def require_second_admin(self, tenant_id: UUID) -> bool:
        """Return whether publication requires a second administrator."""

        ...


@dataclass(frozen=True, slots=True)
class AdminGovernanceHttpRepositories:
    """Ports required by the governance endpoints for one session."""

    publication: GovernancePublicationRepository
    policy: GovernancePolicy
    role: RoleRepository


class AdminGovernanceHttpDependencyProvider(Protocol):
    """Create a request-scoped governance port bundle from a session."""

    def for_session(self, session: object) -> AdminGovernanceHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminCaseTypesHttpDependencyProvider",
    "AdminCaseTypesHttpRepositories",
    "AdminGovernanceHttpDependencyProvider",
    "AdminGovernanceHttpRepositories",
    "GovernancePolicy",
    "GovernancePublicationRepository",
]
