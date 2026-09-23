"""Dependency contracts for the forms HTTP composition boundary.

The forms router needs several ports bound to one request transaction
(case + case-type-version + form binding + form version loader + audit +
outbox). This module describes that bundle without importing FastAPI,
SQLAlchemy, or any infrastructure adapter. The composition root supplies
the concrete provider.

The multi-step session seam (``session``) is satisfied by
``SqlFormSessionRepository`` in production (FM-038); a ``None``
provider keeps the router's 501 fail-closed shape for test doubles
and future seam work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.forms.advanced_execution import FormSessionRepository
from app.domain.form import FormBinding, FormVersion
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    OutboxWriter,
)


class FormsFormVersionRepository(Protocol):
    """Form-version read seam needed by the forms HTTP surface.

    A superset of the submit path's ``FormVersionLoader``: the read
    endpoints also resolve the latest published version by key and list
    published versions for the tenant.
    """

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None:
        """Return one form version by id, scoped to tenant."""

        ...

    async def get_latest_published_for_key(
        self, tenant_id: UUID, form_key: str
    ) -> FormVersion | None:
        """Return the latest published form version for a key."""

        ...

    async def list_published(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Sequence[FormVersion]:
        """Return published form versions for the tenant, newest first."""

        ...


class FormsFormBindingRepository(Protocol):
    """Form-binding read seam needed by the forms HTTP surface.

    A superset of the submit path's ``FormBindingRepository``: the
    bindings endpoint also lists every published binding for one form
    version.
    """

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None:
        """Return the published binding for a form key."""

        ...

    async def list_published_for_form_version(
        self,
        tenant_id: UUID,
        form_version_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[FormBinding]:
        """Return published bindings for a form version."""

        ...


@dataclass(frozen=True, slots=True)
class FormsHttpRepositories:
    """Ports required by the forms HTTP endpoints for one session."""

    case: CaseRepository
    case_type_version: CaseTypeVersionRepository
    form_binding: FormsFormBindingRepository
    form_version: FormsFormVersionRepository
    audit: AuditEventStore
    outbox: OutboxWriter
    session: FormSessionRepository | None


class FormsHttpDependencyProvider(Protocol):
    """Create a request-scoped forms port bundle from a persistence session."""

    def for_session(self, session: object) -> FormsHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "FormsFormBindingRepository",
    "FormsFormVersionRepository",
    "FormsHttpDependencyProvider",
    "FormsHttpRepositories",
]
