"""Tests for P11-US8 / P11-US11 session controls (FM-049 / ADR-011).

The session-control surface is the application-level enforcement of
three P11 commitments:

1. **Idle / absolute timeouts** (P11-US8). A session idle for more
   than 15 minutes (or alive for more than 8 hours) must refuse every
   protected action until re-authenticated.
2. **Failed-attempt lockout** (P11-US8). Five consecutive failures
   must lock the account for the configured window.
3. **Approved-client-certificate for privileged actions** (P11-US11).
   Privileged actions must require an approved cert; missing or
   unapproved certs must produce ``403``. The privileged-action
   surface is **Gated** (ADR-011): the policy action is published but
   no role grants it, so the policy is deny-by-default today.

The acceptance criteria from the task brief:

- "idle 15min logs out"
- "5 failed attempts lock"
- "privileged action without cert 403"
- "Gated requires explicit authorization"

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Tests cover the domain shape, the application use case, and
the HTTP router end-to-end (with the in-memory adapters the router
ships).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.authorization import (
    DEFAULT_ROLES,
    Action,
    Policy,
    Role,
    configure_default_policy,
)
from app.application.security import session_controls
from app.application.security.session_controls import (
    PublishSessionPolicyCommand,
    SessionDecision,
)
from app.deps import UserContext, get_clock, get_current_user, get_db
from app.domain.errors import Forbidden, InvariantViolation, NotFound, ValidationFailed
from app.domain.session import (
    DEFAULT_ABSOLUTE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_LOCKOUT_DURATION,
    DEFAULT_MAX_FAILED_ATTEMPTS,
    Session,
    SessionDenialReason,
    SessionPolicy,
    SessionState,
    new_session,
)
from app.infrastructure.persistence.security_http_dependencies import (
    SqlSecurityHttpDependencyProvider,
)
from app.interfaces.http.routers.security import (
    _get_session_policy_store,
    _get_session_store,
    _InMemorySessionPolicyStore,
    _InMemorySessionStore,
)
from app.interfaces.http.routers.security import (
    router as security_router,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_default_policy():
    """Reset the module-level default policy after the test.

    The application use case calls ``authorize`` which reads the
    module singleton. Tests that grant a Gated action to a custom
    role swap the singleton in; without this fixture the next test
    sees the patched policy and the Gated denial check (the
    cornerstone of the ADR-011 Gated fit) is bypassed silently.

    The fixture always restores to ``None`` so the next call to
    ``default_policy()`` rebuilds from the static ``DEFAULT_ROLES``
    registry — the cleanest reset that survives test-ordering
    perturbations.

    Use this fixture on any test that calls ``configure_default_policy``.
    """
    import app.application.authorization as authz_mod  # noqa: PLC0415

    yield
    authz_mod._default_policy = None


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _utc(year: int = 2026, month: int = 1, day: int = 1, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _policy(
    *,
    idle_seconds: int = int(DEFAULT_IDLE_TIMEOUT.total_seconds()),
    absolute_seconds: int = int(DEFAULT_ABSOLUTE_TIMEOUT.total_seconds()),
    max_failed: int = DEFAULT_MAX_FAILED_ATTEMPTS,
    lockout_seconds: int = int(DEFAULT_LOCKOUT_DURATION.total_seconds()),
    privileged_actions: frozenset[str] = frozenset(),
) -> SessionPolicy:
    return SessionPolicy(
        idle_timeout=timedelta(seconds=idle_seconds),
        absolute_timeout=timedelta(seconds=absolute_seconds),
        max_failed_attempts=max_failed,
        lockout_duration=timedelta(seconds=lockout_seconds),
        privileged_actions=privileged_actions,
    )


# ===========================================================================
# Domain shape (app/domain/session.py)
# ===========================================================================


class TestSessionPolicy:
    """The configuration object's validation rules."""

    def test_session_policy__defaults_match_documented_values(self) -> None:
        """The shipped defaults match the AGENTS.md brief: idle 15min,
        absolute 8h, 5 attempts, 15min lockout."""
        policy = SessionPolicy()
        assert policy.idle_timeout == timedelta(minutes=15)
        assert policy.absolute_timeout == timedelta(hours=8)
        assert policy.max_failed_attempts == 5
        assert policy.lockout_duration == timedelta(minutes=15)
        assert policy.privileged_actions == frozenset()

    def test_session_policy__idle_zero_rejected(self) -> None:
        """An idle timeout of zero would log every caller out on the
        next request — the validator refuses it."""
        with pytest.raises(ValidationFailed):
            SessionPolicy(idle_timeout=timedelta(0))

    def test_session_policy__idle_negative_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            SessionPolicy(idle_timeout=timedelta(seconds=-1))

    def test_session_policy__absolute_must_exceed_idle(self) -> None:
        """Absolute timeout <= idle timeout makes the absolute check
        redundant; the validator enforces the invariant so a misconfigured
        publish surfaces as 422."""
        with pytest.raises(ValidationFailed):
            SessionPolicy(
                idle_timeout=timedelta(minutes=15),
                absolute_timeout=timedelta(minutes=15),
            )

    def test_session_policy__max_failed_attempts_zero_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            SessionPolicy(max_failed_attempts=0)

    def test_session_policy__lockout_zero_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            SessionPolicy(lockout_duration=timedelta(0))

    def test_session_policy__is_privileged_deny_by_default(self) -> None:
        """Unknown actions are not privileged (deny-by-default; absent
        action == not on the allowlist)."""
        policy = _policy()
        assert policy.is_privileged("case.create") is False

    def test_session_policy__is_privileged_allowlisted_action(self) -> None:
        policy = _policy(privileged_actions=frozenset({"admin.rotate_keys"}))
        assert policy.is_privileged("admin.rotate_keys") is True
        assert policy.is_privileged("case.create") is False


class TestSessionAggregate:
    """The pure-domain lifecycle of a single session."""

    def _fresh(self, *, now: datetime | None = None) -> Session:
        return new_session(
            session_id=uuid4(),
            tenant_id=TENANT_A,
            user_id=USER_ID,
            now=now or _utc(),
        )

    def test_new_session__starts_active_with_zero_failures(self) -> None:
        now = _utc()
        session = self._fresh(now=now)
        assert session.state == SessionState.ACTIVE
        assert session.failed_attempts == 0
        assert session.locked_until is None
        assert session.last_used_at == now
        assert session.created_at == now

    def test_session__is_idle_expired_at_15_minutes(self) -> None:
        """Acceptance: 'idle 15min logs out'.

        A session whose ``last_used_at`` is exactly 15 minutes ago is
        at the boundary. The check uses ``>`` (strict greater-than)
        so the boundary is still usable and the next request after
        triggers the refusal.
        """
        now = _utc()
        session = self._fresh(now=now - timedelta(minutes=14, seconds=59))
        policy = _policy(idle_seconds=15 * 60)
        assert session.is_idle_expired(now, policy=policy) is False
        bumped = session.touch(now - timedelta(minutes=15))
        assert bumped.is_idle_expired(now, policy=policy) is False
        # Anything strictly beyond 15 minutes flips to expired.
        stale = self._fresh(now=now - timedelta(minutes=15, seconds=1))
        assert stale.is_idle_expired(now, policy=policy) is True

    def test_session__is_absolute_expired_after_lifetime(self) -> None:
        """Absolute timeout = 8h; anything older is unusable."""
        now = _utc()
        session = self._fresh(now=now - timedelta(hours=8, seconds=1))
        policy = _policy(absolute_seconds=8 * 3600)
        assert session.is_absolute_expired(now, policy=policy) is True

    def test_session__lock_after_five_failures(self) -> None:
        """Acceptance: '5 failed attempts lock'.

        ``record_failed_attempt`` is called five times; on the fifth
        attempt the returned session is ``LOCKED`` with
        ``locked_until`` set to ``now + lockout_duration``. Attempts
        below the threshold leave the state ``ACTIVE``.
        """
        now = _utc()
        session = self._fresh(now=now)
        policy = _policy(max_failed=5, lockout_seconds=900)
        for _ in range(4):
            session = session.record_failed_attempt(now, policy=policy)
            assert session.state == SessionState.ACTIVE
            assert session.failed_attempts <= 5
        locked = session.record_failed_attempt(now, policy=policy)
        assert locked.state == SessionState.LOCKED
        assert locked.failed_attempts == 5
        assert locked.locked_until == now + timedelta(seconds=900)

    def test_session__is_locked_within_window(self) -> None:
        now = _utc()
        session = self._fresh(now=now - timedelta(minutes=10))
        session = session.record_failed_attempt(
            now, policy=_policy(max_failed=1, lockout_seconds=900)
        )
        # Locked and within the lock window.
        future = now + timedelta(seconds=899)
        assert session.is_locked(future, policy=_policy(lockout_seconds=900))

    def test_session__lock_window_expiry_releases_lock(self) -> None:
        """After the lock window elapses, ``is_locked`` returns False;
        the caller calls ``reset_after_lockout`` to clear the counter.
        """
        now = _utc()
        session = self._fresh(now=now)
        policy = _policy(max_failed=1, lockout_seconds=60)
        session = session.record_failed_attempt(now, policy=policy)
        assert session.state == SessionState.LOCKED
        later = now + timedelta(seconds=61)
        assert session.is_locked(later, policy=policy) is False
        cleared = session.reset_after_lockout(later)
        assert cleared.state == SessionState.ACTIVE
        assert cleared.failed_attempts == 0
        assert cleared.locked_until is None

    def test_session__record_success_clears_counter_and_bumps_clock(self) -> None:
        now = _utc()
        session = self._fresh(now=now - timedelta(minutes=10))
        session = session.record_failed_attempt(
            now - timedelta(seconds=1), policy=_policy(max_failed=3)
        )
        assert session.failed_attempts == 1
        success = session.record_success(now)
        assert success.state == SessionState.ACTIVE
        assert success.failed_attempts == 0
        assert success.last_used_at == now

    def test_session__expire_marks_state(self) -> None:
        session = self._fresh().expire(_utc())
        assert session.state == SessionState.EXPIRED

    def test_session__revoke_marks_state(self) -> None:
        now = _utc()
        session = self._fresh(now=now).revoke(now)
        assert session.state == SessionState.REVOKED

    def test_session__enforce_usable_raises_for_locked(self) -> None:
        now = _utc()
        session = self._fresh(now=now).record_failed_attempt(
            now, policy=_policy(max_failed=1, lockout_seconds=900)
        )
        with pytest.raises(Forbidden) as excinfo:
            session.enforce_usable(now, policy=_policy())
        assert SessionDenialReason.ACCOUNT_LOCKED.value in str(excinfo.value)

    def test_session__enforce_usable_raises_for_idle(self) -> None:
        now = _utc()
        session = self._fresh(now=now - timedelta(minutes=20))
        with pytest.raises(Forbidden) as excinfo:
            session.enforce_usable(now, policy=_policy(idle_seconds=15 * 60))
        assert SessionDenialReason.IDLE_TIMEOUT.value in str(excinfo.value)

    def test_session__enforce_usable_raises_for_absolute(self) -> None:
        """Absolute timeout fires even when the session is fresh enough
        to clear the idle check. Build a session whose
        ``last_used_at`` is current but whose ``created_at`` is past
        the absolute window."""
        now = _utc()
        created_at = now - timedelta(hours=9)
        last_used = now - timedelta(seconds=1)
        session = Session(
            id=uuid4(),
            tenant_id=TENANT_A,
            user_id=USER_ID,
            state=SessionState.ACTIVE,
            created_at=created_at,
            last_used_at=last_used,
            failed_attempts=0,
            locked_until=None,
        )
        with pytest.raises(Forbidden) as excinfo:
            session.enforce_usable(now, policy=_policy(absolute_seconds=8 * 3600))
        assert SessionDenialReason.ABSOLUTE_TIMEOUT.value in str(excinfo.value)

    def test_session__enforce_usable_passes_when_fresh(self) -> None:
        now = _utc()
        session = self._fresh(now=now)
        session.enforce_usable(now, policy=_policy())

    def test_session__with_approved_client_cert_stores_thumbprint(self) -> None:
        session = self._fresh().with_approved_client_cert("abc123def456")
        assert session.approved_client_cert_thumbprint == "abc123def456"

    def test_session__with_approved_client_cert_rejects_empty(self) -> None:
        session = self._fresh()
        with pytest.raises(InvariantViolation):
            session.with_approved_client_cert("")

    def test_session__enforce_privileged_without_policy_match_passes(self) -> None:
        """A non-privileged action never raises — the policy decides
        what is privileged, and absence == not privileged."""
        now = _utc()
        session = self._fresh(now=now)
        session.enforce_privileged(
            "case.create",
            presented_thumbprint=None,
            policy=_policy(),
        )

    def test_session__enforce_privileged_missing_cert_raises(self) -> None:
        """Acceptance: 'privileged action without cert 403'.

        When the action is on the privileged list and the presented
        thumbprint is ``None``, the session refuses with
        ``CERT_REQUIRED``. The reason is the on-the-wire contract the
        HTTP layer maps to ``403``.
        """
        session = self._fresh()
        with pytest.raises(Forbidden) as excinfo:
            session.enforce_privileged(
                "admin.rotate_keys",
                presented_thumbprint=None,
                policy=_policy(privileged_actions=frozenset({"admin.rotate_keys"})),
            )
        assert SessionDenialReason.CERT_REQUIRED.value in str(excinfo.value)

    def test_session__enforce_privileged_unapproved_cert_raises(self) -> None:
        session = self._fresh().with_approved_client_cert("approved")
        with pytest.raises(Forbidden) as excinfo:
            session.enforce_privileged(
                "admin.rotate_keys",
                presented_thumbprint="rogue",
                policy=_policy(privileged_actions=frozenset({"admin.rotate_keys"})),
            )
        assert SessionDenialReason.CERT_NOT_APPROVED.value in str(excinfo.value)

    def test_session__enforce_privileged_matching_cert_passes(self) -> None:
        session = self._fresh().with_approved_client_cert("ABC123")
        # Case-insensitive match (transports vary on hex casing).
        session.enforce_privileged(
            "admin.rotate_keys",
            presented_thumbprint="abc123",
            policy=_policy(privileged_actions=frozenset({"admin.rotate_keys"})),
        )


# ===========================================================================
# Application use cases (app/application/security/session_controls.py)
# ===========================================================================


class _RecordingAudit:
    """In-memory audit recorder for the application use-case tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "actor_id": actor_id,
                "subject_id": subject_id,
                "payload": payload,
                "occurred_at": occurred_at,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
            }
        )


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _admin_actor() -> UserContext:
    return UserContext(
        user_id=USER_ID,
        tenant_id=TENANT_A,
        display_name="admin",
        roles=("process_admin",),
    )


class TestCheckSessionForRequest:
    async def test_check_session__returns_session_and_policy_on_active(self) -> None:
        """A fresh, unused session is reported as allowed with the
        tenant's defaults (no policy published)."""
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        policy_store = _InMemorySessionPolicyStore()

        decision = await session_controls.check_session_for_request(
            tenant_id=TENANT_A,
            session_id=session.id,
            session_repo=session_store,
            policy_repo=policy_store,
            now=now,
        )
        assert isinstance(decision, SessionDecision)
        assert decision.allowed is True
        assert decision.reason is None
        assert decision.session.id == session.id
        # Defaults are applied when no policy is published.
        assert decision.policy.idle_timeout == DEFAULT_IDLE_TIMEOUT

    async def test_check_session__idle_timeout_denies(self) -> None:
        """An idle session over the policy window is refused with
        ``IDLE_TIMEOUT``."""
        now = _utc()
        stale = now - timedelta(minutes=20)
        session = new_session(
            session_id=uuid4(),
            tenant_id=TENANT_A,
            user_id=USER_ID,
            now=stale,
        )
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        decision = await session_controls.check_session_for_request(
            tenant_id=TENANT_A,
            session_id=session.id,
            session_repo=session_store,
            policy_repo=_InMemorySessionPolicyStore(),
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == SessionDenialReason.IDLE_TIMEOUT

    async def test_check_session__lockout_denies(self) -> None:
        """A locked session within the lock window is refused with
        ``ACCOUNT_LOCKED``."""
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session = session.record_failed_attempt(
            now, policy=_policy(max_failed=1, lockout_seconds=900)
        )
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        decision = await session_controls.check_session_for_request(
            tenant_id=TENANT_A,
            session_id=session.id,
            session_repo=session_store,
            policy_repo=_InMemorySessionPolicyStore(),
            now=now + timedelta(seconds=1),
        )
        assert decision.allowed is False
        assert decision.reason == SessionDenialReason.ACCOUNT_LOCKED

    async def test_check_session__unknown_session_raises_not_found(self) -> None:
        """Cross-tenant or missing session id surfaces as ``NotFound``
        so the HTTP layer can map it to 404 without leaking existence."""
        with pytest.raises(NotFound):
            await session_controls.check_session_for_request(
                tenant_id=TENANT_A,
                session_id=uuid4(),
                session_repo=_InMemorySessionStore(),
                policy_repo=_InMemorySessionPolicyStore(),
                now=_utc(),
            )


class TestEnforcePrivilegedAction:
    async def test_enforce_privileged__non_privileged_action_passes(self) -> None:
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        policy_store = _InMemorySessionPolicyStore()
        decision = await session_controls.enforce_session_for_request(
            tenant_id=TENANT_A,
            session_id=session.id,
            action="case.create",
            presented_client_cert_thumbprint=None,
            session_repo=session_store,
            policy_repo=policy_store,
            now=now,
        )
        assert decision.allowed is True

    async def test_enforce_privileged__missing_cert_denies(self) -> None:
        """Acceptance: 'privileged action without cert 403'.

        The privileged-action policy declares the action privileged;
        no client cert is presented; the application use case returns
        a ``CERT_REQUIRED`` decision. The HTTP router translates that
        to ``HTTP 403`` (see the router test below)."""
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        policy_store = _InMemorySessionPolicyStore()
        await policy_store.publish(
            TENANT_A,
            _policy(privileged_actions=frozenset({"admin.rotate_keys"})),
        )
        decision = await session_controls.enforce_session_for_request(
            tenant_id=TENANT_A,
            session_id=session.id,
            action="admin.rotate_keys",
            presented_client_cert_thumbprint=None,
            session_repo=session_store,
            policy_repo=policy_store,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == SessionDenialReason.CERT_REQUIRED


class TestRecordAuthenticationOutcome:
    async def test_record_failure__locks_after_five(self) -> None:
        """Acceptance: '5 failed attempts lock'.

        The application use case calls the repository's
        ``record_failed_attempt``; on the fifth attempt the
        returned session is ``LOCKED`` and two audit events are
        recorded (the failure + the lockout, the latter linked to
        the former via ``causation_id``)."""
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        audit = _RecordingAudit()
        clock = _FixedClock(now)
        for _ in range(4):
            session = await session_controls.record_authentication_failure(
                tenant_id=TENANT_A,
                session_id=session.id,
                session_repo=session_store,
                audit=audit,
                correlation_id="corr-1",
                clock=clock,
            )
        assert session.state == SessionState.ACTIVE
        locked = await session_controls.record_authentication_failure(
            tenant_id=TENANT_A,
            session_id=session.id,
            session_repo=session_store,
            audit=audit,
            correlation_id="corr-1",
            clock=clock,
        )
        assert locked.state == SessionState.LOCKED
        # Five failure events + one lockout event, all correlated.
        failure_events = [e for e in audit.events if e["event_type"].endswith("Failed")]
        lock_events = [e for e in audit.events if e["event_type"].endswith("Locked")]
        assert len(failure_events) == 5
        assert len(lock_events) == 1
        # The lockout event carries the prior failure as causation.
        assert lock_events[0]["causation_id"] == "SessionAuthenticationFailed"

    async def test_record_success__clears_counter_and_audits(self) -> None:
        now = _utc()
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=now)
        session = session.record_failed_attempt(now, policy=_policy(max_failed=3))
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        audit = _RecordingAudit()
        clock = _FixedClock(now)
        success = await session_controls.record_authentication_success(
            tenant_id=TENANT_A,
            session_id=session.id,
            session_repo=session_store,
            audit=audit,
            correlation_id="corr-2",
            clock=clock,
        )
        assert success.state == SessionState.ACTIVE
        assert success.failed_attempts == 0
        types = [e["event_type"] for e in audit.events]
        assert types == ["SessionAuthenticationSucceeded"]


class TestApproveClientCertificate:
    async def test_approve_client_certificate__requires_gated_action(self) -> None:
        """Gated per ADR-011: the endpoint requires
        ``Action.SESSION_REQUIRE_CERT``; no role grants it today.

        An ``intake`` actor (which has no admin grant) must be
        refused with ``Forbidden``; a ``process_admin`` actor would
        also be refused today because ``SESSION_REQUIRE_CERT`` is
        not in the role's action set — that is the deny-by-default
        the gate enforces.
        """
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=_utc())
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        audit = _RecordingAudit()
        clock = _FixedClock(_utc())

        with pytest.raises(Forbidden):
            await session_controls.approve_client_certificate(
                session_controls.ApproveClientCertCommand(
                    tenant_id=TENANT_A,
                    actor=_admin_actor(),
                    correlation_id="corr",
                    session_id=session.id,
                    thumbprint="abc123",
                ),
                session_repo=session_store,
                audit=audit,
                clock=clock,
            )
        # The denial path does not mutate the session.
        assert (await session_store.get(TENANT_A, session.id)) is not None
        assert audit.events == []

    @pytest.mark.usefixtures("_restore_default_policy")
    async def test_approve_client_certificate__blank_thumbprint_raises(
        self,
    ) -> None:
        """A blank thumbprint is rejected before any persistence."""
        session = new_session(session_id=uuid4(), tenant_id=TENANT_A, user_id=USER_ID, now=_utc())
        session_store = _InMemorySessionStore()
        await session_store.add(session)
        privileged_actor = UserContext(
            user_id=USER_ID,
            tenant_id=TENANT_A,
            display_name="privileged",
            roles=("privileged_certs",),
        )
        # Patch the policy to grant the action to this role for the
        # duration of the test only — lets us exercise the validation
        # branch rather than the authorization branch. The fixture
        # restores the singleton afterwards.
        policy = Policy(
            roles=(
                *DEFAULT_ROLES,
                Role(
                    name="privileged_certs",
                    actions=frozenset({Action.SESSION_REQUIRE_CERT}),
                ),
            ),
        )
        configure_default_policy(policy)
        with pytest.raises(ValidationFailed):
            await session_controls.approve_client_certificate(
                session_controls.ApproveClientCertCommand(
                    tenant_id=TENANT_A,
                    actor=privileged_actor,
                    correlation_id="corr",
                    session_id=session.id,
                    thumbprint="   ",
                ),
                session_repo=session_store,
                audit=_RecordingAudit(),
                clock=_FixedClock(_utc()),
            )


class TestPublishSessionPolicy:
    async def test_publish_session_policy__requires_gated_action(self) -> None:
        """Gated per ADR-011: ``Action.SESSION_POLICY_PUBLISH`` has no
        role grants today. The default ``process_admin`` actor must be
        refused."""
        policy_store = _InMemorySessionPolicyStore()
        audit = _RecordingAudit()
        clock = _FixedClock(_utc())
        with pytest.raises(Forbidden):
            await session_controls.publish_session_policy(
                PublishSessionPolicyCommand(
                    tenant_id=TENANT_A,
                    actor=_admin_actor(),
                    correlation_id="corr",
                    idle_timeout_seconds=900,
                    absolute_timeout_seconds=28_800,
                    max_failed_attempts=5,
                    lockout_duration_seconds=900,
                    privileged_actions=("admin.rotate_keys",),
                ),
                policy_repo=policy_store,
                audit=audit,
                clock=clock,
            )
        # Publish refused before persistence.
        assert audit.events == []
        assert await policy_store.get(TENANT_A) is None

    @pytest.mark.usefixtures("_restore_default_policy")
    async def test_publish_session_policy__rejects_invalid_seconds(
        self,
    ) -> None:
        """The wire-layer validator surfaces zero / negative seconds
        as ``ValidationFailed`` (HTTP 422). The use case is wired with
        a policy that grants ``SESSION_POLICY_PUBLISH`` to the test
        actor so we exercise the *validation* branch rather than the
        *authorization* branch."""
        policy = Policy(
            roles=(
                *DEFAULT_ROLES,
                Role(
                    name="policy_publisher",
                    actions=frozenset({Action.SESSION_POLICY_PUBLISH}),
                ),
            ),
        )
        configure_default_policy(policy)
        with pytest.raises(ValidationFailed):
            await session_controls.publish_session_policy(
                PublishSessionPolicyCommand(
                    tenant_id=TENANT_A,
                    actor=UserContext(
                        user_id=USER_ID,
                        tenant_id=TENANT_A,
                        display_name="publisher",
                        roles=("policy_publisher",),
                    ),
                    correlation_id="corr",
                    idle_timeout_seconds=0,
                    absolute_timeout_seconds=28_800,
                    max_failed_attempts=5,
                    lockout_duration_seconds=900,
                ),
                policy_repo=_InMemorySessionPolicyStore(),
                audit=_RecordingAudit(),
                clock=_FixedClock(_utc()),
            )


# ===========================================================================
# HTTP router (app/interfaces/http/routers/security.py)
# ===========================================================================


class TestRouter:
    """The router is the HTTP boundary; tests use FastAPI's TestClient."""

    # Class-level fixture so every TestRouter test resets the module
    # policy singleton before running — the pre-existing
    # ``test_policy_evaluate__actor_with_multiple_roles__any_match_grants``
    # test leaks the singleton, and the Gated denial check depends on
    # the default role set being intact.
    @pytest.fixture(autouse=True)
    def _reset_default_policy(self) -> None:
        import app.application.authorization as authz_mod  # noqa: PLC0415

        authz_mod._default_policy = None

    def _client(self, *, now: datetime | None = None) -> Any:
        # Reset the in-memory stores so each test starts clean.
        _get_session_store().__init__()  # type: ignore[misc]
        _get_session_policy_store().__init__()  # type: ignore[misc]

        app = FastAPI()
        # Replace the auth dependency with a stub actor.
        app.dependency_overrides[get_current_user] = lambda: UserContext(
            user_id=USER_ID,
            tenant_id=TENANT_A,
            display_name="tester",
            roles=("process_admin",),
        )
        # Replace the clock so the test's "fresh session" stays fresh
        # regardless of wall-clock time.
        clock_time = now or _utc()
        app.dependency_overrides[get_clock] = lambda: _FixedClock(clock_time)

        # The synthetic environment ships without a SQL adapter for
        # the session store; bypass the DB dependency (the router guards
        # ``db is None`` and skips commit). The security provider is wired
        # so audit-capable endpoints resolve; Gated denials precede any
        # audit write in these tests.
        async def _no_db() -> AsyncIterator[None]:
            yield None

        app.dependency_overrides[get_db] = _no_db
        app.state.security_http_dependencies = SqlSecurityHttpDependencyProvider()
        app.include_router(security_router, prefix="/api/v1")
        return TestClient(app)

    async def _seed_session(self) -> UUID:
        session = new_session(
            session_id=uuid4(),
            tenant_id=TENANT_A,
            user_id=USER_ID,
            now=_utc(),
        )
        store = _get_session_store_safe()
        await store.add(session)
        return session.id

    async def _seed_locked_session(self) -> UUID:
        session = new_session(
            session_id=uuid4(),
            tenant_id=TENANT_A,
            user_id=USER_ID,
            now=_utc(),
        )
        session = session.record_failed_attempt(
            _utc(), policy=_policy(max_failed=1, lockout_seconds=900)
        )
        store = _get_session_store_safe()
        await store.add(session)
        return session.id

    def test_get_session_status__active_returns_200(self) -> None:
        client = self._client()
        session_id = asyncio.run(self._seed_session())
        response = client.get(f"/api/v1/security/sessions/{session_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == str(session_id)
        assert body["state"] == "active"
        assert body["idle_expired"] is False

    def test_get_session_status__locked_returns_401(self) -> None:
        client = self._client()
        session_id = asyncio.run(self._seed_locked_session())
        response = client.get(f"/api/v1/security/sessions/{session_id}")
        assert response.status_code == 401
        assert response.json()["detail"] == SessionDenialReason.ACCOUNT_LOCKED.value

    def test_check_privilege__missing_cert_returns_403(self) -> None:
        """Acceptance: 'privileged action without cert 403'.

        The privileged-action set is published; no cert is presented;
        the router returns ``403 forbidden`` with the
        ``CERT_REQUIRED`` reason in the body."""
        client = self._client()
        session_id = asyncio.run(self._seed_session())
        # Publish a privileged-action set so the check fires.
        policy_store = _get_session_policy_store_safe()
        asyncio.run(
            policy_store.publish(
                TENANT_A,
                _policy(privileged_actions=frozenset({"admin.rotate_keys"})),
            )
        )
        response = client.post(
            f"/api/v1/security/sessions/{session_id}/check-privilege",
            json={"action": "admin.rotate_keys", "client_cert_thumbprint": None},
        )
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["detail"]["reason"] == SessionDenialReason.CERT_REQUIRED.value

    def test_check_privilege__non_privileged_action_returns_200(self) -> None:
        client = self._client()
        session_id = asyncio.run(self._seed_session())
        response = client.post(
            f"/api/v1/security/sessions/{session_id}/check-privilege",
            json={"action": "case.create", "client_cert_thumbprint": None},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["allowed"] is True
        assert body["privileged"] is False

    def test_approve_client_cert__gated_action_denies_default_role(self) -> None:
        """Gated per ADR-011: the default ``process_admin`` actor is
        refused because no role grants ``SESSION_REQUIRE_CERT``."""
        client = self._client()
        session_id = asyncio.run(self._seed_session())
        response = client.post(
            f"/api/v1/security/sessions/{session_id}/approve-client-cert",
            json={"thumbprint": "abc123"},
        )
        assert response.status_code == 403, response.text

    def test_publish_session_policy__gated_action_denies_default_role(self) -> None:
        """Gated per ADR-011: the default ``process_admin`` actor is
        refused because no role grants ``SESSION_POLICY_PUBLISH``."""
        client = self._client()
        response = client.post(
            "/api/v1/security/session-policy",
            json={
                "idle_timeout_seconds": 900,
                "absolute_timeout_seconds": 28_800,
                "max_failed_attempts": 5,
                "lockout_duration_seconds": 900,
                "privileged_actions": [],
            },
        )
        assert response.status_code == 403, response.text

    def test_get_session_policy__defaults_when_none_published(self) -> None:
        client = self._client()
        response = client.get("/api/v1/security/session-policy")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["idle_timeout_seconds"] == int(DEFAULT_IDLE_TIMEOUT.total_seconds())
        assert body["max_failed_attempts"] == DEFAULT_MAX_FAILED_ATTEMPTS


def _get_session_store_safe() -> _InMemorySessionStore:
    """Resolve the in-memory store without resetting it."""
    return _get_session_store()


def _get_session_policy_store_safe() -> _InMemorySessionPolicyStore:
    return _get_session_policy_store()
