"""Connector + CoexistencePolicy ports (P14-US5, US7 / FM-058, FM-059).

The connector framework is the integration seam for enterprise
systems (AGENTS.md §12). The application use cases own the lifecycle
(approve / enable / monitor / retire / drain) and the repository
owns the durable row. Every method is tenant-scoped (Constitution V).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.coexistence import CoexistencePolicy
from app.domain.connector import Connector
from app.domain.ports.core import TenantScoped


class ConnectorRepository(TenantScoped, Protocol):
    """Persistence port for :class:`Connector` aggregates (FM-058).

    A connector row is the durable record of "we have authorized
    connector X for tenant Y in state Z". The lifecycle transitions
    are issued by the application layer; the repository is the
    single mutation surface. Tenant isolation is mandatory: every
    method takes ``tenant_id`` first and the SQL adapter MUST include
    ``WHERE tenant_id = :tenant_id`` on every query (AGENTS.md §3 R1).
    """

    async def get(self, tenant_id: UUID, connector_id: UUID) -> Connector | None:
        """Return the connector row, or ``None`` for missing or
        cross-tenant ids. The two cases are intentionally
        indistinguishable so the API never confirms cross-tenant
        existence (Constitution V)."""

    async def get_by_key(self, tenant_id: UUID, key: str) -> Connector | None:
        """Return the connector with the given operator-facing key.

        Keys are tenant-scoped (two tenants may each have a connector
        named ``"synthetic"`` without collision). Returns ``None`` for
        missing or cross-tenant lookups."""

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Connector]:
        """List connectors for the tenant, newest first."""

    async def add(self, connector: Connector) -> None:
        """Persist a freshly drafted connector."""

    async def update(self, connector: Connector) -> None:
        """Persist a mutated connector (state transition, drain
        confirmation). The aggregate is the source of truth; the
        repository only stores the snapshot."""


# ---------------------------------------------------------------------------
# Coexistence policy port (P14-US6 / FM-059)
#
# A coexistence policy is the governance artifact a process
# administrator records to declare "this tenant treats legacy system X
# as the source of record Y; real-time sync is OFF; ownership rule Z
# applies". The aggregate is tenant-scoped; cross-tenant reads return
# ``None`` so the API never confirms cross-tenant existence
# (Constitution V).
# ---------------------------------------------------------------------------


class CoexistencePolicyRepository(TenantScoped, Protocol):
    """Persistence port for :class:`CoexistencePolicy` aggregates (FM-059).

    Every method takes ``tenant_id`` first and the SQL adapter MUST
    include ``WHERE tenant_id = :tenant_id`` on every query (AGENTS.md
    §3 R1). The repository is the single mutation surface; the
    application layer's use cases own the lifecycle.
    """

    async def get(self, tenant_id: UUID, policy_id: UUID) -> CoexistencePolicy | None:
        """Return the policy row, or ``None`` for missing or
        cross-tenant ids. The two cases are intentionally
        indistinguishable so the API never confirms cross-tenant
        existence (Constitution V)."""

    async def get_by_key(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
        """Return the policy with the given operator-facing key.

        Keys are tenant-scoped (two tenants may each have a policy
        named ``"onbase-inbound"`` without collision). Returns
        ``None`` for missing or cross-tenant lookups."""

    async def get_latest_version(self, tenant_id: UUID, key: str) -> CoexistencePolicy | None:
        """Return the policy with the highest ``version`` for ``key``,

        or ``None`` if no policy exists for the tenant at that key.
        Used by the application layer to compute the next ``version``
        on draft and by the API surface to surface "current policy"."""

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CoexistencePolicy]:
        """List policies for the tenant, newest first."""

    async def add(self, policy: CoexistencePolicy) -> None:
        """Persist a freshly drafted policy."""

    async def update(self, policy: CoexistencePolicy) -> None:
        """Persist a mutated policy (state transition, evidence
        append). The aggregate is the source of truth; the repository
        only stores the snapshot."""


__all__ = [
    "ConnectorRepository",
    "CoexistencePolicyRepository",
]
