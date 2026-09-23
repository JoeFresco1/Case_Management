"""Role + assignment ports (P11-US4 / FM-048).

Authorization is evaluated live against these stores so that revoking
a role takes effect on the very next request -- there is no caching
layer in the policy. The Policy port below hides the lookup shape from
``app.application.authorization.Policy``; the adapter in
``app.infrastructure.persistence.repositories.role_repository``
satisfies it via SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.role import Role
from app.domain.session import Session, SessionPolicy


class RoleRepository(TenantScoped, Protocol):
    """Role aggregate + assignment persistence.

    Every method is tenant-scoped. ``permissions_for_user`` is the live
    read path used by the policy; revoking a role + flushing the
    transaction is enough to make ``permissions_for_user`` reflect the
    change on the next call (no cache invalidation needed).
    """

    async def get(self, tenant_id: UUID, role_id: UUID) -> Role | None: ...

    async def get_by_name(self, tenant_id: UUID, name: str) -> Role | None: ...

    async def list(self, tenant_id: UUID) -> Sequence[Role]: ...

    async def add(self, role: Role) -> None: ...

    async def update_permissions(
        self, tenant_id: UUID, role_id: UUID, permissions: frozenset[str]
    ) -> None: ...

    async def delete(self, tenant_id: UUID, role_id: UUID) -> None: ...

    async def assign(
        self,
        tenant_id: UUID,
        user_id: UUID,
        role_id: UUID,
        *,
        assigned_by: UUID | None,
        assigned_at: datetime,
    ) -> None: ...

    async def revoke(self, tenant_id: UUID, user_id: UUID, role_id: UUID) -> None: ...

    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]:
        """Live union of all ``Action.value`` strings granted to ``user_id``."""


# ---------------------------------------------------------------------------
# Session security ports (P11-US8 / P11-US11 / FM-049 / ADR-011)
#
# These are deliberately separate from the policy + role ports above:
# they describe the *durable* shape of a session (counter, idle clock,
# approved cert reference) and of the tenant-scoped session policy
# (idle window, lockout threshold, privileged-action allowlist). The
# application use case in ``app.application.security.session_controls``
# consults both on every protected request and is the only thing that
# mutates them.
# ---------------------------------------------------------------------------


class SessionRepository(TenantScoped, Protocol):
    """Per-session durability (P11-US8 / FM-049).

    The repository owns the row that survives across requests so the
    application does not need to keep session state in memory. The
    ``approve_client_certificate`` write is the seam where the
    privileged-access-workstation policy lands when the production
    target is selected (ADR-011 Gated).

    All methods are tenant-scoped: a session id from tenant A cannot
    match in tenant B. Implementations must include ``WHERE
    tenant_id = :tenant_id`` on every SQL.
    """

    async def get(self, tenant_id: UUID, session_id: UUID) -> Session | None:
        """Return the session for ``session_id`` in ``tenant_id``.

        Returns ``None`` for a missing id OR for an id that belongs
        to a different tenant -- the two cases are intentionally
        indistinguishable so the API never confirms cross-tenant
        existence (Constitution V).
        """

    async def get_active_for_user(self, tenant_id: UUID, user_id: UUID) -> list[Session]:
        """Return every active (non-revoked) session for ``user_id``.

        Used by the operator console to enumerate concurrent sessions
        on a lockout event. The HTTP layer paginates the list; the
        repository returns the full set so it can sort by ``last_used_at``.
        """

    async def add(self, session: Session) -> None:
        """Persist a freshly authenticated session."""

    async def save(self, session: Session) -> None:
        """Persist a mutated session.

        Called by the application use case after every state change
        (touch, failed attempt, lockout, cert approval). Implementations
        MUST compare-and-swap on the row's ``last_used_at`` / state so a
        concurrent request from the same session does not silently
        overwrite a freshly recorded lockout.
        """

    async def record_failed_attempt(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None:
        """Atomically increment the failed-attempt counter and persist
        the new session row. When the counter crosses the policy
        threshold the returned session is in the ``LOCKED`` state with
        ``locked_until`` set; otherwise the state is unchanged.

        Returns ``None`` when the session is missing or cross-tenant
        so the caller can surface a 401 without leaking existence.
        """

    async def record_success(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None:
        """Atomically clear the failed-attempt counter, reset the
        state to ``ACTIVE``, and bump ``last_used_at`` to ``now``.

        Returns ``None`` when the session is missing or cross-tenant.
        """


class SessionPolicyRepository(TenantScoped, Protocol):
    """Tenant-scoped ``SessionPolicy`` durability (P11-US8 / FM-049).

    The repository holds the published configuration: idle timeout,
    absolute timeout, max failed attempts, lockout duration, and the
    privileged-action allowlist. There is exactly one row per tenant;
    publishing replaces it.

    Publish is reserved for ``Action.SESSION_POLICY_PUBLISH`` (Gated
    per ADR-011) -- no role grants this action today.
    """

    async def get(self, tenant_id: UUID) -> SessionPolicy | None:
        """Return the tenant's session policy, or ``None`` if none is
        published yet. The application use case falls back to the
        domain defaults when ``None`` is returned so a tenant that
        never published a policy still gets the safe defaults.
        """

    async def publish(self, tenant_id: UUID, policy: SessionPolicy) -> None:
        """Replace the tenant's published session policy.

        Implementations MUST audit the change in the same transaction
        so a successful publish is durable and traceable.
        """


__all__ = [
    "RoleRepository",
    "SessionRepository",
    "SessionPolicyRepository",
]
