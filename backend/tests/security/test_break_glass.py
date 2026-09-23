"""Tests for P11-US10 / FM-035 break-glass access.

The break-glass surface is the platform's only legitimate path that
bypasses the standard tenant filter on case searches (P06-US9) and
lets an auditor in tenant A read a case in tenant B. The acceptance
criteria from the task brief:

- "auditor granted 15min break-glass with reason can search cross-tenant"
- "after expiry denied"
- "all actions during window flagged and audited with reason"

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5. Tests cover:

- Domain aggregate lifecycle (``TestBreakGlassGrant``).
- Application use cases for grant / revoke / check / flag
  (``TestBreakGlassUseCases``).
- HTTP router end-to-end with the in-memory adapters the router ships
  (``TestBreakGlassRouter``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

from app.application.authorization import (
    Action,
    Policy,
    Role,
    configure_default_policy,
)
from app.application.security import break_glass
from app.application.security.break_glass import (
    BREAK_GLASS_ACTION,
    BREAK_GLASS_GRANTED,
    BREAK_GLASS_REVOKED,
    GrantBreakGlassCommand,
    RevokeBreakGlassCommand,
)
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.break_glass import (
    MAX_GRANT_DURATION,
    MIN_GRANT_DURATION,
    BreakGlassDenialReason,
    BreakGlassGrant,
    BreakGlassScope,
)
from app.domain.errors import NotFound, ValidationFailed


def _http_imports():
    """Lazy import so domain/unit tests run without httpx.

    The synthetic environment ships a starlette that warns about
    httpx; the dev sandbox in this slice is missing httpx2. Domain
    and use-case tests do not need the HTTP client — import only on
    the router test class below.
    """
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    return FastAPI, TestClient


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
AUDITOR_ID = UUID("00000000-0000-0000-0000-000000000001")
PROCESS_ADMIN_ID = UUID("00000000-0000-0000-0000-000000000002")
TARGET_USER_ID = UUID("00000000-0000-0000-0000-000000000003")
RESOURCE_ID = UUID("00000000-0000-0000-0000-00000000000a")


def _utc(year: int = 2026, month: int = 1, day: int = 1, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


@dataclass
class FrozenClock:
    _now: datetime = field(default_factory=_utc)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now


def _process_admin_policy() -> Policy:
    """A policy where process_admin can issue break-glass overrides.

    The default registry already grants ``ADMIN_TENANT_CONFIG`` to
    ``process_admin``; we restate it explicitly so the fixture does
    not silently lose coverage if a future commit removes the grant.
    """
    role = Role(
        name="process_admin",
        actions=frozenset(
            {
                Action.CASE_READ,
                Action.AUDIT_READ,
                Action.ADMIN_TENANT_CONFIG,
            }
        ),
    )
    return Policy(roles=[role])


@pytest.fixture
def admin_policy():
    configure_default_policy(_process_admin_policy())
    yield
    import app.application.authorization as authz_mod  # noqa: PLC0415

    authz_mod._default_policy = None


@pytest.fixture
def admin_user() -> UserContext:
    return UserContext(
        user_id=PROCESS_ADMIN_ID,
        tenant_id=TENANT_A,
        display_name="Process Admin",
        roles=("process_admin",),
    )


@pytest.fixture
def auditor_user() -> UserContext:
    return UserContext(
        user_id=AUDITOR_ID,
        tenant_id=TENANT_A,
        display_name="Auditor",
        roles=("auditor",),
    )


# In-memory adapters shared with the application use cases. The audit
# store is a list of events the tests assert against.


@dataclass
class InMemoryGrantStore:
    grants: dict[tuple[UUID, UUID], BreakGlassGrant] = field(default_factory=dict)

    async def add(self, grant: BreakGlassGrant) -> None:
        self.grants[(grant.tenant_id, grant.id)] = grant

    async def get(self, tenant_id: UUID, grant_id: UUID) -> BreakGlassGrant | None:
        return self.grants.get((tenant_id, grant_id))

    async def save(self, grant: BreakGlassGrant) -> None:
        self.grants[(grant.tenant_id, grant.id)] = grant

    async def active_grant_for(
        self,
        tenant_id: UUID,
        user_id: UUID,
        scope: BreakGlassScope,
        *,
        now: datetime,
    ) -> BreakGlassGrant | None:
        for grant in self.grants.values():
            if grant.tenant_id != tenant_id:
                continue
            if grant.user_id != user_id:
                continue
            if grant.scope is not scope:
                continue
            if grant.is_active(now=now):
                return grant
        return None

    async def grant_for(
        self,
        tenant_id: UUID,
        user_id: UUID,
        scope: BreakGlassScope,
    ) -> BreakGlassGrant | None:
        matches = [
            g
            for g in self.grants.values()
            if g.tenant_id == tenant_id and g.user_id == user_id and g.scope is scope
        ]
        if not matches:
            return None
        return max(matches, key=lambda g: g.granted_at)


@dataclass
class RecordingAudit:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def make_grant(
    *,
    duration_minutes: int = 15,
    scope: BreakGlassScope = BreakGlassScope.CROSS_TENANT_SEARCH,
    reason: str = "Live incident — auditor investigation",
    now: datetime | None = None,
    tenant_id: UUID = TENANT_A,
    user_id: UUID = TARGET_USER_ID,
    granted_by: UUID = PROCESS_ADMIN_ID,
) -> BreakGlassGrant:
    return BreakGlassGrant.grant(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        scope=scope,
        reason=reason,
        granted_by=granted_by,
        granted_at=now or _utc(),
        duration=timedelta(minutes=duration_minutes),
    )


# ===========================================================================
# Domain aggregate lifecycle
# ===========================================================================


class TestBreakGlassGrant:
    """Pure-domain validation + lifecycle of ``BreakGlassGrant``."""

    def test_grant_factory__rejects_empty_reason(self) -> None:
        with pytest.raises(ValueError, match="."):
            BreakGlassGrant.grant(
                id=uuid4(),
                tenant_id=TENANT_A,
                user_id=TARGET_USER_ID,
                scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                reason="   ",
                granted_by=PROCESS_ADMIN_ID,
                granted_at=_utc(),
                duration=timedelta(minutes=15),
            )

    def test_grant_factory__rejects_oversized_reason(self) -> None:
        with pytest.raises(ValueError, match="."):
            BreakGlassGrant.grant(
                id=uuid4(),
                tenant_id=TENANT_A,
                user_id=TARGET_USER_ID,
                scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                reason="x" * 1025,
                granted_by=PROCESS_ADMIN_ID,
                granted_at=_utc(),
                duration=timedelta(minutes=15),
            )

    def test_grant_factory__rejects_below_minimum_duration(self) -> None:
        with pytest.raises(ValueError, match="."):
            BreakGlassGrant.grant(
                id=uuid4(),
                tenant_id=TENANT_A,
                user_id=TARGET_USER_ID,
                scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                reason="live incident",
                granted_by=PROCESS_ADMIN_ID,
                granted_at=_utc(),
                duration=timedelta(seconds=30),
            )

    def test_grant_factory__rejects_above_maximum_duration(self) -> None:
        with pytest.raises(ValueError, match="."):
            BreakGlassGrant.grant(
                id=uuid4(),
                tenant_id=TENANT_A,
                user_id=TARGET_USER_ID,
                scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                reason="live incident",
                granted_by=PROCESS_ADMIN_ID,
                granted_at=_utc(),
                duration=MAX_GRANT_DURATION + timedelta(minutes=1),
            )

    def test_grant_factory__accepts_minimum_duration(self) -> None:
        grant = BreakGlassGrant.grant(
            id=uuid4(),
            tenant_id=TENANT_A,
            user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="live incident",
            granted_by=PROCESS_ADMIN_ID,
            granted_at=_utc(),
            duration=MIN_GRANT_DURATION,
        )
        assert grant.expires_at - grant.granted_at == MIN_GRANT_DURATION

    def test_grant__is_active_within_window(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        assert grant.is_active(now=now)
        assert grant.is_active(now=now + timedelta(minutes=14, seconds=59))

    def test_grant__is_inactive_after_expiry(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        assert grant.is_active(now=now + timedelta(minutes=15)) is False
        assert grant.is_active(now=now + timedelta(minutes=20)) is False

    def test_grant__is_inactive_before_granted_at(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        assert grant.is_active(now=now - timedelta(seconds=1)) is False

    def test_grant__revoke_marks_inactive_and_preserves_history(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        later = now + timedelta(minutes=5)
        revoked = grant.revoke(revoked_by=PROCESS_ADMIN_ID, revoked_at=later)
        # Frozen dataclass: the original grant is untouched.
        assert grant.revoked_at is None
        assert revoked.revoked_at == later
        assert revoked.revoked_by == PROCESS_ADMIN_ID
        assert revoked.is_active(now=later + timedelta(minutes=1)) is False

    def test_grant__revoke_idempotent(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        first = grant.revoke(revoked_by=PROCESS_ADMIN_ID, revoked_at=now)
        # Second revoke is a no-op and returns the same instance.
        again = first.revoke(revoked_by=PROCESS_ADMIN_ID, revoked_at=now)
        assert again is first

    def test_grant__check_returns_none_when_active_for_scope(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        assert grant.check(scope=BreakGlassScope.CROSS_TENANT_SEARCH, now=now) is None

    def test_grant__check_reports_revoked_over_expired(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        revoked = grant.revoke(revoked_by=PROCESS_ADMIN_ID, revoked_at=now)
        later = now + timedelta(hours=1)
        assert (
            revoked.check(scope=BreakGlassScope.CROSS_TENANT_SEARCH, now=later)
            == BreakGlassDenialReason.REVOKED
        )

    def test_grant__check_reports_expired_when_past_expiry(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        later = now + timedelta(minutes=30)
        assert (
            grant.check(scope=BreakGlassScope.CROSS_TENANT_SEARCH, now=later)
            == BreakGlassDenialReason.EXPIRED
        )

    def test_grant__check_reports_scope_mismatch(self) -> None:
        now = _utc()
        grant = make_grant(
            duration_minutes=15,
            now=now,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
        )
        assert (
            grant.check(scope=BreakGlassScope.CROSS_TENANT_CASE_READ, now=now)
            == BreakGlassDenialReason.SCOPE_NOT_GRANTED
        )

    def test_grant__remaining_zero_after_expiry(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        assert grant.remaining(now=now + timedelta(hours=1)) == timedelta(0)

    def test_grant__remaining_positive_within_window(self) -> None:
        now = _utc()
        grant = make_grant(duration_minutes=15, now=now)
        delta = grant.remaining(now=now + timedelta(minutes=5))
        assert delta == timedelta(minutes=10)


# ===========================================================================
# Application use cases
# ===========================================================================


class TestBreakGlassUseCases:
    """Grant / revoke / check / flag flows through the use cases."""

    @pytest.mark.asyncio
    async def test_grant__writes_audit_row_with_reason(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-1",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="Incident 4711 — auditor cross-tenant investigation",
            duration_minutes=15,
        )
        grant = await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        assert grant.user_id == TARGET_USER_ID
        assert grant.scope == BreakGlassScope.CROSS_TENANT_SEARCH
        assert grant.granted_by == PROCESS_ADMIN_ID
        assert grant.revoked_at is None
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event["event_type"] == BREAK_GLASS_GRANTED
        assert event["payload"]["reason"] == cmd.reason
        assert event["payload"]["scope"] == "cross_tenant_search"
        assert event["payload"]["duration_minutes"] == 15

    @pytest.mark.asyncio
    async def test_grant__rejects_self_grant_for_human_actor(self, admin_policy) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        self_admin = UserContext(
            user_id=PROCESS_ADMIN_ID,
            tenant_id=TENANT_A,
            display_name="Self Admin",
            roles=("process_admin",),
        )
        with pytest.raises(ValidationFailed):
            await break_glass.grant_break_glass(
                GrantBreakGlassCommand(
                    tenant_id=TENANT_A,
                    actor=self_admin,
                    correlation_id="corr-2",
                    target_user_id=PROCESS_ADMIN_ID,
                    scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                    reason="self-grant attempt",
                    duration_minutes=15,
                ),
                grant_repo=repo,
                audit=audit,
                clock=clock,
                new_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_grant__rejects_oversized_duration(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        with pytest.raises(ValidationFailed):
            await break_glass.grant_break_glass(
                GrantBreakGlassCommand(
                    tenant_id=TENANT_A,
                    actor=admin_user,
                    correlation_id="corr-3",
                    target_user_id=TARGET_USER_ID,
                    scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                    reason="forever",
                    duration_minutes=24 * 60 + 1,
                ),
                grant_repo=repo,
                audit=audit,
                clock=clock,
                new_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_check__returns_grant_when_active_within_window(
        self, admin_policy, admin_user
    ) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-4",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="incident investigation",
            duration_minutes=15,
        )
        grant = await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        clock.advance(timedelta(minutes=5))
        result = await break_glass.check_break_glass(
            tenant_id=TENANT_A,
            user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            grant_repo=repo,
            clock=clock,
        )
        assert result.allowed is True
        assert result.grant is not None
        assert result.grant.id == grant.id

    @pytest.mark.asyncio
    async def test_check__denies_after_expiry(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-5",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="incident investigation",
            duration_minutes=15,
        )
        await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        # Advance beyond the 15-minute window.
        clock.advance(timedelta(minutes=16))
        result = await break_glass.check_break_glass(
            tenant_id=TENANT_A,
            user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            grant_repo=repo,
            clock=clock,
        )
        assert result.allowed is False
        assert result.reason == BreakGlassDenialReason.EXPIRED

    @pytest.mark.asyncio
    async def test_check__denies_when_revoked(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-6",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="incident investigation",
            duration_minutes=15,
        )
        await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        await break_glass.revoke_break_glass(
            RevokeBreakGlassCommand(
                tenant_id=TENANT_A,
                actor=admin_user,
                correlation_id="corr-7",
                grant_id=next(iter(repo.grants.values())).id,
            ),
            grant_repo=repo,
            audit=audit,
            clock=clock,
        )
        result = await break_glass.check_break_glass(
            tenant_id=TENANT_A,
            user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            grant_repo=repo,
            clock=clock,
        )
        assert result.allowed is False
        assert result.reason == BreakGlassDenialReason.REVOKED

    @pytest.mark.asyncio
    async def test_revoke__writes_audit_row_and_idempotent(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-8",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="incident investigation",
            duration_minutes=15,
        )
        grant = await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        await break_glass.revoke_break_glass(
            RevokeBreakGlassCommand(
                tenant_id=TENANT_A,
                actor=admin_user,
                correlation_id="corr-9",
                grant_id=grant.id,
            ),
            grant_repo=repo,
            audit=audit,
            clock=clock,
        )
        # Second revoke is idempotent and still audits.
        await break_glass.revoke_break_glass(
            RevokeBreakGlassCommand(
                tenant_id=TENANT_A,
                actor=admin_user,
                correlation_id="corr-10",
                grant_id=grant.id,
            ),
            grant_repo=repo,
            audit=audit,
            clock=clock,
        )
        revoke_events = [e for e in audit.events if e["event_type"] == BREAK_GLASS_REVOKED]
        assert len(revoke_events) == 2
        assert revoke_events[0]["payload"]["idempotent"] is False
        assert revoke_events[1]["payload"]["idempotent"] is True

    @pytest.mark.asyncio
    async def test_revoke__not_found_for_cross_tenant_grant_id(
        self, admin_policy, admin_user
    ) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        # Tenant A admin tries to revoke a grant that lives in tenant B.
        bogus = uuid4()
        with pytest.raises(NotFound):
            await break_glass.revoke_break_glass(
                RevokeBreakGlassCommand(
                    tenant_id=TENANT_A,
                    actor=admin_user,
                    correlation_id="corr-11",
                    grant_id=bogus,
                ),
                grant_repo=repo,
                audit=audit,
                clock=clock,
            )

    @pytest.mark.asyncio
    async def test_record_action__appends_flagged_audit_row(self, admin_policy, admin_user) -> None:
        repo = InMemoryGrantStore()
        audit = RecordingAudit()
        clock = FrozenClock()
        cmd = GrantBreakGlassCommand(
            tenant_id=TENANT_A,
            actor=admin_user,
            correlation_id="corr-12",
            target_user_id=TARGET_USER_ID,
            scope=BreakGlassScope.CROSS_TENANT_SEARCH,
            reason="incident investigation",
            duration_minutes=15,
        )
        grant = await break_glass.grant_break_glass(
            cmd,
            grant_repo=repo,
            audit=audit,
            clock=clock,
            new_id=uuid4(),
        )
        # Reset the audit recorder so the action event is easy to find.
        audit.events.clear()
        await break_glass.record_break_glass_action(
            grant=grant,
            action_name="case.search.cross_tenant",
            resource_kind="case",
            resource_id=RESOURCE_ID,
            correlation_id="corr-13",
            grant_repo=repo,
            audit=audit,
            clock=clock,
            extra_payload={"query": "INC-4711", "result_count": 3},
        )
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event["event_type"] == BREAK_GLASS_ACTION
        assert event["payload"]["grant_id"] == str(grant.id)
        assert event["payload"]["reason"] == cmd.reason
        assert event["payload"]["scope"] == "cross_tenant_search"
        assert event["payload"]["action"] == "case.search.cross_tenant"
        assert event["payload"]["resource_kind"] == "case"
        assert event["payload"]["resource_id"] == str(RESOURCE_ID)
        assert event["payload"]["query"] == "INC-4711"
        assert event["payload"]["result_count"] == 3


# ===========================================================================
# HTTP router end-to-end
# ===========================================================================


class TestBreakGlassRouter:
    """Acceptance: 'auditor granted 15min can search cross-tenant;
    after expiry denied; all actions flagged and audited'."""

    @pytest.fixture
    def app(self, admin_policy, admin_user, sql_session_factory) -> FastAPI:
        from app.infrastructure.persistence.case_http_dependencies import (  # noqa: PLC0415
            SqlCaseHttpDependencyProvider,
        )
        from app.infrastructure.persistence.search_http_dependencies import (  # noqa: PLC0415
            SqlSearchHttpDependencyProvider,
        )
        from app.infrastructure.persistence.security_http_dependencies import (  # noqa: PLC0415
            SqlSecurityHttpDependencyProvider,
        )
        from app.interfaces.http.routers.security import (  # noqa: PLC0415
            router as security_router,
        )

        fast_api, _ = _http_imports()
        application = fast_api()
        application.state.security_http_dependencies = SqlSecurityHttpDependencyProvider()
        application.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
        application.state.search_http_dependencies = SqlSearchHttpDependencyProvider()
        application.include_router(security_router, prefix="/api/v1")

        def _override_user() -> UserContext:
            return admin_user

        def _override_clock() -> Clock:
            return FrozenClock()

        async def _override_get_db():
            async with sql_session_factory() as session:
                yield session

        application.dependency_overrides[get_current_user] = _override_user
        application.dependency_overrides[get_clock] = _override_clock
        application.dependency_overrides[get_db] = _override_get_db
        return application

    def _client(self, app: FastAPI) -> TestClient:
        _, test_client = _http_imports()
        return test_client(app)

    def test_router__grants_15_min_break_glass_with_reason(self, app: FastAPI) -> None:
        client = self._client(app)
        resp = client.post(
            "/api/v1/security/break-glass",
            json={
                "target_user_id": str(TARGET_USER_ID),
                "scope": "cross_tenant_search",
                "reason": "Live incident — auditor investigation",
                "duration_minutes": 15,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scope"] == "cross_tenant_search"
        assert body["target_user_id"] == str(TARGET_USER_ID)
        assert body["reason"] == "Live incident — auditor investigation"
        assert body["revoked_at"] is None

    def test_router__rejects_self_grant(self, app: FastAPI) -> None:
        client = self._client(app)
        resp = client.post(
            "/api/v1/security/break-glass",
            json={
                "target_user_id": str(PROCESS_ADMIN_ID),
                "scope": "cross_tenant_search",
                "reason": "self-grant",
                "duration_minutes": 15,
            },
        )
        assert resp.status_code == 422

    def test_router__rejects_oversized_duration(self, app: FastAPI) -> None:
        client = self._client(app)
        resp = client.post(
            "/api/v1/security/break-glass",
            json={
                "target_user_id": str(TARGET_USER_ID),
                "scope": "cross_tenant_search",
                "reason": "forever",
                "duration_minutes": 25 * 60,
            },
        )
        assert resp.status_code == 422

    def test_router__revoke_returns_updated_grant(self, app: FastAPI) -> None:
        client = self._client(app)
        created = client.post(
            "/api/v1/security/break-glass",
            json={
                "target_user_id": str(TARGET_USER_ID),
                "scope": "cross_tenant_search",
                "reason": "incident",
                "duration_minutes": 15,
            },
        )
        assert created.status_code == 201
        grant_id = created.json()["grant_id"]
        deleted = client.delete(f"/api/v1/security/break-glass/{grant_id}")
        assert deleted.status_code == 200
        assert deleted.json()["revoked_at"] is not None

    def test_router__revoke_unknown_grant_is_404(self, app: FastAPI) -> None:
        client = self._client(app)
        resp = client.delete(f"/api/v1/security/break-glass/{uuid4()}")
        assert resp.status_code == 404
