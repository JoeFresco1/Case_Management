"""Local governance policy adapter (FM-021 / P10-US8).

The policy answers one question per tenant: "does a publish require a
second administrator's approval?". The local adapter returns
``False`` for every tenant today (single-admin is sufficient in the
local synthetic environment). The future slice that wires the admin
UI to flip the flag on a tenant will persist the setting on
``tenants`` (or a sibling ``tenant_settings`` row) and replace this
adapter with a SQL-backed implementation behind the same Protocol.

The adapter is intentionally module-level state-free: the
:class:`LocalGovernancePolicy` instance is constructed on each
request and the answer is deterministic from the constructor
arguments. The :class:`InMemoryGovernancePolicy` test double lives
in the application test fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class LocalGovernancePolicy:
    """Default local adapter — no tenant requires second-admin approval.

    A future slice may override individual tenants via the
    ``require_second_admin_overrides`` dict (an explicit per-tenant
    allowlist). The default is deny-by-default on the override set so
    a freshly created tenant does not silently enable the gate.
    """

    require_second_admin_overrides: dict[UUID, bool] = field(default_factory=dict)

    async def require_second_admin(self, tenant_id: UUID) -> bool:
        """Return ``True`` iff this tenant has second-admin approval on."""
        return bool(self.require_second_admin_overrides.get(tenant_id, False))


__all__ = ["LocalGovernancePolicy"]
