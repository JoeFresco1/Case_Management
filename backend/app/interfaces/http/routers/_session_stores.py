"""Process-local in-memory session and session-policy stores (synthetic seam).

# INTEGRATION_SEAM: local adapter only — the production/Azure adapter replaces
# these with the SQL ``SessionRepository`` / ``SessionPolicyRepository`` ports
# declared in ``app/domain/ports.py``. The router and application layers depend
# on the port shape, so swapping the adapter needs no endpoint changes.

The first slice ships in-memory stores so the synthetic environment can
exercise the full session-control surface without a database. They are
process-singletons: the synthetic environment runs single-process by design,
and a SQL adapter lands alongside the SQL ``RoleRepository`` in a later slice.

``security.py`` re-exports these names so existing imports
(``from app.interfaces.http.routers.security import _InMemorySessionStore``)
keep resolving unchanged.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.domain.session import Session, SessionPolicy, SessionState


class _InMemorySessionStore:
    """Process-local session store.

    Tenant-scoped on every method: a session id from tenant A cannot
    match in tenant B. Concurrent access is not safe — the synthetic
    environment runs single-process by design.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[UUID, UUID], Session] = {}

    async def get(self, tenant_id: UUID, session_id: UUID) -> Session | None:
        return self._sessions.get((tenant_id, session_id))

    async def get_active_for_user(self, tenant_id: UUID, user_id: UUID) -> list[Session]:
        return [
            s
            for (tid, _), s in self._sessions.items()
            if tid == tenant_id and s.user_id == user_id and s.state != SessionState.REVOKED
        ]

    async def add(self, session: Session) -> None:
        self._sessions[(session.tenant_id, session.id)] = session

    async def save(self, session: Session) -> None:
        self._sessions[(session.tenant_id, session.id)] = session

    async def record_failed_attempt(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None:
        session = self._sessions.get((tenant_id, session_id))
        if session is None:
            return None
        policy = SessionPolicy()  # defaults; the in-memory store does
        # not consult the published policy because the lockout
        # threshold is itself a configuration value. The real
        # adapter passes the tenant's published policy in.
        updated = session.record_failed_attempt(now, policy=policy)
        self._sessions[(tenant_id, session_id)] = updated
        return updated

    async def record_success(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None:
        session = self._sessions.get((tenant_id, session_id))
        if session is None:
            return None
        updated = session.record_success(now)
        self._sessions[(tenant_id, session_id)] = updated
        return updated


class _InMemorySessionPolicyStore:
    """Process-local session policy store."""

    def __init__(self) -> None:
        self._policies: dict[UUID, SessionPolicy] = {}

    async def get(self, tenant_id: UUID) -> SessionPolicy | None:
        return self._policies.get(tenant_id)

    async def publish(self, tenant_id: UUID, policy: SessionPolicy) -> None:
        self._policies[tenant_id] = policy


_session_store_singleton: _InMemorySessionStore | None = None
_session_policy_store_singleton: _InMemorySessionPolicyStore | None = None


def _get_session_store() -> _InMemorySessionStore:
    """Return the process-singleton in-memory session store.

    The synthetic environment is single-process; the SQL adapter
    replaces this in a future slice and reads from the per-request
    session via ``Depends(get_db)``.
    """
    global _session_store_singleton  # noqa: PLW0603
    if _session_store_singleton is None:
        _session_store_singleton = _InMemorySessionStore()
    return _session_store_singleton


def _get_session_policy_store() -> _InMemorySessionPolicyStore:
    global _session_policy_store_singleton  # noqa: PLW0603
    if _session_policy_store_singleton is None:
        _session_policy_store_singleton = _InMemorySessionPolicyStore()
    return _session_policy_store_singleton
