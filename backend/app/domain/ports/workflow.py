"""Workflow configuration ports (P02-US2 / FM-013 / FR-015).

The workflow configuration lifecycle mirrors the case-type configuration
lifecycle: ``draft -> validate -> publish -> retire``. The application use
cases in ``app.application.workflow`` orchestrate across the WorkflowDefinition
header, the WorkflowVersion snapshot, the configured queues, and the
append-only audit chain. The SQL adapters for these ports land in a
follow-up slice (T066-T077) — until then, the application layer is
exercised through in-memory fakes and the HTTP seam is mounted against
the ``NotImplementedError`` stubs in
``app.infrastructure.persistence.repositories`` (AGENTS.md §12 — every
seam must be typed, located, and runtime-checkable before the real
adapter ships).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)


class WorkflowDefinitionRepository(TenantScoped, Protocol):
    """WorkflowDefinition header persistence (FM-013 / FR-015).

    Only the WorkflowDefinition header methods the draft/publish use
    cases actually call. Existing workflow instances remain bound to
    their original version (P02 pillar §2); the current_version pointer
    is the **effective** version new workflow instances pin at creation
    time.
    """

    async def get(self, tenant_id: UUID, workflow_definition_id: UUID) -> WorkflowDefinition | None:
        """Fetch one WorkflowDefinition by id, scoped to tenant.

        Returns None for missing OR cross-tenant ids.
        """

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None:
        """Fetch the WorkflowDefinition by its stable ``key``."""

    async def set_current_version(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_id: UUID
    ) -> None:
        """Promote ``version_id`` to the WorkflowDefinition's current version.

        Called by the publish use case after the version row has been
        flipped to ``published``. Implementations MUST compare-and-swap
        on the row's existing ``current_version_id`` semantics so a
        concurrent publish cannot silently overwrite a freshly recorded
        promotion (Constitution V).
        """


class WorkflowVersionRepository(TenantScoped, Protocol):
    """WorkflowVersion aggregate persistence (FM-013 / FR-015).

    Provides:

    - ``get`` / ``add`` / ``mark_status`` -- read/write primitives.
    - ``get_latest_for_workflow`` -- used by the draft use case to compute
      the next ``version_number``.

    ``mark_status`` enforces the lifecycle (``draft -> published`` or
    ``published -> retired``); the use case constructs the new domain
    entity via :meth:`WorkflowVersion.publish` / :meth:`retire` first
    and the repository applies the status flip.
    """

    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None:
        """Fetch one WorkflowVersion by id, scoped to tenant."""

    async def add(self, version: WorkflowVersion) -> None:
        """Persist a freshly drafted WorkflowVersion."""

    async def get_latest_for_workflow(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowVersion | None:
        """Return the most-recent version regardless of status.

        The draft use case computes ``next_version_number = latest + 1``
        (or 1 if no prior version exists).
        """

    async def get_by_version_number(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_number: int
    ) -> WorkflowVersion | None:
        """Fetch a specific ``WorkflowVersion`` by its monotonic number.

        Used by the workflow simulator endpoint
        (``POST /admin/workflows/{id}/versions/{n}/simulate``,
        FM-015 / P02-US5) which addresses versions by the
        author-visible ``version_number`` rather than the internal id.
        Implementations scope by tenant and return ``None`` for missing
        or cross-tenant lookups.
        """

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: WorkflowVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        """Transition a WorkflowVersion's status atomically.

        Implementations MUST reject any transition other than
        ``draft -> published`` or ``published -> retired``. This is the
        persistence-layer complement to the domain's
        :meth:`WorkflowVersion.publish` / :meth:`retire` immutability
        guards (FR-015).
        """


__all__ = [
    "WorkflowDefinitionRepository",
    "WorkflowVersionRepository",
]
