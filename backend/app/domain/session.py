"""Session security controls (P11-US8 / P11-US11 / FM-049).

This module is pure domain: no I/O, no FastAPI, no SQLAlchemy. It owns
the **shape** of three session-related policies the security
administrator can configure and the application layer enforces:

1. **Idle + absolute session limits** (P11-US8). A session that has
   been idle for more than ``idle_timeout`` minutes, or that has been
   alive for more than ``absolute_timeout`` minutes, is **expired**.
   Expired sessions refuse every protected action until re-authenticated.
2. **Failed-attempt lockout** (P11-US8). After
   ``max_failed_attempts`` consecutive authentication failures for a
   given subject key, the account is **locked** for
   ``lockout_duration`` minutes. Locked accounts refuse every protected
   action — including the one that would otherwise clear the lock —
   until the lockout window expires.
3. **Approved-client-certificate requirement** (P11-US11). Privileged
   actions require the request to carry a client certificate whose
   thumbprint is in the tenant's approved set. This is **Gated**:
   the surface is reserved for the privileged-access-workstation
   policy and the configuration is deny-by-default until a product
   decision is made. No role today is granted ``Action.SESSION_REQUIRE_CERT``;
   see ``ADR-011``.

# Why a separate module from ``authorization.py``

Authorization decides *what* a caller may do. Session controls decide
*whether* the caller may do anything at all. Conflating them would force
the policy registry to know about clock skew and cert stores; keeping
them in distinct layers lets the policy stay declarative and lets the
session checks compose cleanly with ``policy.evaluate``.

# Why a separate aggregate from ``User``

``User.tenant_id`` is the platform identity. ``Session`` is the
transport-level identity: a single user may have many concurrent
sessions, each with its own idle clock and (when required) its own
client certificate. The session is the unit the failed-attempt counter
and the idle clock hang off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from app.domain.errors import Forbidden, InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Policy defaults — overridable per-tenant via configuration but shipped as
# constants so the test fixture and the production code agree.
# ---------------------------------------------------------------------------

DEFAULT_IDLE_TIMEOUT: Final[timedelta] = timedelta(minutes=15)
DEFAULT_ABSOLUTE_TIMEOUT: Final[timedelta] = timedelta(hours=8)
DEFAULT_MAX_FAILED_ATTEMPTS: Final[int] = 5
DEFAULT_LOCKOUT_DURATION: Final[timedelta] = timedelta(minutes=15)

# Privileged actions that (P11-US11) require an approved client
# certificate when the policy is enabled. Today the set is empty —
# adding an action here is the migration step that activates the
# privileged-access-workstation policy. See ADR-011.
DEFAULT_PRIVILEGED_ACTIONS: Final[frozenset[str]] = frozenset()

# ---------------------------------------------------------------------------
# Failure reasons — stable strings the HTTP layer maps to status codes and
# the audit layer records verbatim. NEVER raise with sensitive content;
# the names below are the contract.
# ---------------------------------------------------------------------------


class SessionDenialReason(StrEnum):
    """Stable denial reasons for session-level refusals.

    The string values are the on-the-wire contract. Adding a new value
    is a wire-incompatible change; deprecating a value requires
    acceptance on both client and server.
    """

    IDLE_TIMEOUT = "idle_timeout"
    ABSOLUTE_TIMEOUT = "absolute_timeout"
    ACCOUNT_LOCKED = "account_locked"
    CERT_REQUIRED = "client_certificate_required"
    CERT_NOT_APPROVED = "client_certificate_not_approved"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Tenant-scoped session-control configuration.

    Why frozen: the policy is published and audited; an in-place
    mutation would silently widen or tighten the lockout window. The
    use case wraps ``publish`` so a new value lands on the audit chain
    in the same transaction as the configuration publish.

    All durations are positive. The validator rejects zero / negative
    values so a malformed config row cannot silently disable the
    control (lockout of zero would never lock; idle timeout of zero
    would log everyone out immediately on the next request).
    """

    idle_timeout: timedelta = DEFAULT_IDLE_TIMEOUT
    absolute_timeout: timedelta = DEFAULT_ABSOLUTE_TIMEOUT
    max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS
    lockout_duration: timedelta = DEFAULT_LOCKOUT_DURATION
    privileged_actions: frozenset[str] = field(default_factory=lambda: DEFAULT_PRIVILEGED_ACTIONS)

    def __post_init__(self) -> None:
        if self.idle_timeout <= timedelta(0):
            raise ValidationFailed("idle_timeout must be > 0", field="idle_timeout")
        if self.absolute_timeout <= self.idle_timeout:
            raise ValidationFailed(
                "absolute_timeout must be > idle_timeout",
                field="absolute_timeout",
            )
        if self.max_failed_attempts <= 0:
            raise ValidationFailed("max_failed_attempts must be > 0", field="max_failed_attempts")
        if self.lockout_duration <= timedelta(0):
            raise ValidationFailed("lockout_duration must be > 0", field="lockout_duration")
        for action in self.privileged_actions:
            if not isinstance(action, str) or not action:
                raise ValidationFailed(
                    f"invalid privileged action: {action!r}",
                    field="privileged_actions",
                )

    def is_privileged(self, action: str) -> bool:
        """Return True when ``action`` requires an approved client cert.

        Unknown actions are not privileged by default — the absent
        action is not on the allowlist. This is the same
        deny-by-default shape ``app.application.authorization.Policy``
        uses for role grants.
        """
        return action in self.privileged_actions


# ---------------------------------------------------------------------------
# Session aggregate
# ---------------------------------------------------------------------------


class SessionState(StrEnum):
    """The lifecycle of a session.

    - ``ACTIVE`` — usable; subject to idle / absolute timeout checks.
    - ``LOCKED`` — temporarily refused due to lockout.
    - ``EXPIRED`` — refused because the idle or absolute window
      elapsed. Distinguished from ``LOCKED`` so the audit timeline can
      report the cause.
    - ``REVOKED`` — explicitly terminated (logout, break-glass
      eviction). The session id remains in the durable record for
      audit but the session is no longer usable.
    """

    ACTIVE = "active"
    LOCKED = "locked"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class Session:
    """A single transport-level identity.

    A session is created at authentication time and carries enough
    state for the application layer to answer the three P11 questions:

    - **Idle**: ``last_used_at`` is bumped on every authenticated
      request; ``is_idle_expired(now, policy)`` returns True when
      ``now - last_used_at > policy.idle_timeout``.
    - **Lockout**: ``failed_attempts`` counts consecutive failures;
      ``record_failed_attempt`` returns a fresh ``Session`` whose
      state is ``LOCKED`` when the threshold trips. ``is_locked(now,
      policy)`` distinguishes a lock whose window has elapsed (the
      caller decides whether to reset the counter).
    - **Cert**: the optional ``approved_client_cert_thumbprint`` is
      checked by ``enforce_privileged`` when ``policy`` declares the
      action privileged. The cert fingerprint is **never** logged in
      audit payloads — it is a reference, not a value (AGENTS.md §18).
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    state: SessionState
    created_at: Any  # datetime
    last_used_at: Any  # datetime
    failed_attempts: int = 0
    locked_until: Any | None = None  # datetime | None
    approved_client_cert_thumbprint: str | None = None

    def __post_init__(self) -> None:
        if self.failed_attempts < 0:
            raise InvariantViolation("failed_attempts must be >= 0")
        if self.approved_client_cert_thumbprint is not None and not isinstance(
            self.approved_client_cert_thumbprint, str
        ):
            raise InvariantViolation("approved_client_cert_thumbprint must be a string when set")

    # ------------------------------------------------------------------
    # Pure queries (no I/O)
    # ------------------------------------------------------------------

    def is_locked(self, now: Any, *, policy: SessionPolicy) -> bool:
        """Return True when the session is locked at ``now``.

        A lock whose ``locked_until`` has elapsed is no longer in
        force; the caller calls ``reset_after_lockout`` to clear the
        counter and resume. The session itself does not mutate state
        in this method — the application use case is the boundary
        that decides when to commit a state change.
        """
        if self.state != SessionState.LOCKED:
            return False
        if self.locked_until is None:
            # Defensive: a locked session must carry a window. If the
            # durable row is malformed we treat it as locked until
            # the next admin intervention rather than silently
            # granting access.
            return True
        now_dt = _coerce_datetime(now)
        locked_until_dt = _coerce_datetime(self.locked_until)
        return now_dt < locked_until_dt

    def is_idle_expired(self, now: Any, *, policy: SessionPolicy) -> bool:
        """Return True when the idle window has elapsed since ``last_used_at``."""
        now_dt = _coerce_datetime(now)
        last_used_dt = _coerce_datetime(self.last_used_at)
        return (now_dt - last_used_dt) > policy.idle_timeout

    def is_absolute_expired(self, now: Any, *, policy: SessionPolicy) -> bool:
        """Return True when the absolute (max-lifetime) window has elapsed."""
        now_dt = _coerce_datetime(now)
        created_dt = _coerce_datetime(self.created_at)
        return (now_dt - created_dt) > policy.absolute_timeout

    def is_expired(self, now: Any, *, policy: SessionPolicy) -> bool:
        """Return True when the session is past either timeout window."""
        return self.is_idle_expired(now, policy=policy) or self.is_absolute_expired(
            now, policy=policy
        )

    def is_usable(self, now: Any, *, policy: SessionPolicy) -> bool:
        """Return True when the session can serve a request right now.

        A session is usable when it is ``ACTIVE`` AND not idle- or
        absolute-expired AND not currently locked. Revoked sessions
        are never usable.
        """
        if self.state != SessionState.ACTIVE:
            return False
        if self.is_expired(now, policy=policy):
            return False
        return not self.is_locked(now, policy=policy)

    # ------------------------------------------------------------------
    # Pure transitions — return a new ``Session`` value
    # ------------------------------------------------------------------

    def touch(self, now: Any) -> Session:
        """Return a copy whose ``last_used_at`` is bumped to ``now``.

        Called on every authenticated request. Does not change the
        state — expiry / lockout are detected on the next read.
        """
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=self.state,
            created_at=self.created_at,
            last_used_at=now,
            failed_attempts=self.failed_attempts,
            locked_until=self.locked_until,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def record_failed_attempt(self, now: Any, *, policy: SessionPolicy) -> Session:
        """Return a copy whose failure counter has been incremented.

        When the counter crosses ``policy.max_failed_attempts``, the
        returned session is ``LOCKED`` with ``locked_until`` set to
        ``now + policy.lockout_duration``. The caller commits the
        change along with the audit row in one transaction.
        """
        next_attempts = self.failed_attempts + 1
        if next_attempts >= policy.max_failed_attempts:
            return Session(
                id=self.id,
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                state=SessionState.LOCKED,
                created_at=self.created_at,
                last_used_at=self.last_used_at,
                failed_attempts=next_attempts,
                locked_until=now + policy.lockout_duration,
                approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
            )
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=self.state,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            failed_attempts=next_attempts,
            locked_until=self.locked_until,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def record_success(self, now: Any) -> Session:
        """Return a copy whose failure counter has been cleared.

        Called on a successful authentication. The state is reset to
        ``ACTIVE`` and the idle clock is bumped to ``now`` so the
        caller does not need a separate ``touch``.
        """
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=SessionState.ACTIVE,
            created_at=self.created_at,
            last_used_at=now,
            failed_attempts=0,
            locked_until=None,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def expire(self, now: Any) -> Session:
        """Return a copy marked ``EXPIRED``."""
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=SessionState.EXPIRED,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            failed_attempts=self.failed_attempts,
            locked_until=self.locked_until,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def revoke(self, now: Any) -> Session:
        """Return a copy marked ``REVOKED`` (explicit logout)."""
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=SessionState.REVOKED,
            created_at=self.created_at,
            last_used_at=now,
            failed_attempts=self.failed_attempts,
            locked_until=self.locked_until,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def reset_after_lockout(self, now: Any) -> Session:
        """Return a copy with the lockout cleared and counter reset.

        Called after ``is_locked`` reports False (the lock window
        elapsed). The session becomes ``ACTIVE`` again and the
        failure counter starts over so a real attacker who only just
        stopped cannot keep a stale counter alive.
        """
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=SessionState.ACTIVE,
            created_at=self.created_at,
            last_used_at=now,
            failed_attempts=0,
            locked_until=None,
            approved_client_cert_thumbprint=self.approved_client_cert_thumbprint,
        )

    def with_approved_client_cert(self, thumbprint: str) -> Session:
        """Return a copy whose approved client cert thumbprint is recorded.

        ``thumbprint`` is the SHA-256 hex digest of the client's X.509
        certificate; callers (the HTTP layer) normalize whatever the
        transport exposes into a stable lowercase hex string before
        calling this method.
        """
        if not thumbprint or not isinstance(thumbprint, str):
            raise InvariantViolation("thumbprint must be a non-empty string")
        return Session(
            id=self.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            state=self.state,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            failed_attempts=self.failed_attempts,
            locked_until=self.locked_until,
            approved_client_cert_thumbprint=thumbprint,
        )

    # ------------------------------------------------------------------
    # Enforcement — raises ``Forbidden`` with a stable reason
    # ------------------------------------------------------------------

    def enforce_usable(self, now: Any, *, policy: SessionPolicy) -> None:
        """Raise ``Forbidden`` with the precise reason if the session
        is not usable right now.

        Order matters: lockout is reported before timeout so an
        attacker who is locked out does not learn the idle window by
        reading the error message.
        """
        if self.state == SessionState.REVOKED:
            raise Forbidden("session revoked")
        if self.is_locked(now, policy=policy):
            raise Forbidden(f"session locked: {SessionDenialReason.ACCOUNT_LOCKED.value}")
        if self.state == SessionState.EXPIRED:
            raise Forbidden(f"session expired: {SessionDenialReason.ABSOLUTE_TIMEOUT.value}")
        if self.is_idle_expired(now, policy=policy):
            raise Forbidden(f"session idle: {SessionDenialReason.IDLE_TIMEOUT.value}")
        if self.is_absolute_expired(now, policy=policy):
            raise Forbidden(f"session expired: {SessionDenialReason.ABSOLUTE_TIMEOUT.value}")
        if self.state != SessionState.ACTIVE:
            # Defensive: future states default to deny.
            raise Forbidden("session not active")

    def enforce_privileged(
        self,
        action: str,
        presented_thumbprint: str | None,
        *,
        policy: SessionPolicy,
    ) -> None:
        """Raise ``Forbidden`` when ``action`` requires an approved
        client certificate and the presented cert is not approved.

        - Unknown action + no privileged policy -> not privileged, no
          check runs. The action's authorization is the policy's job.
        - Privileged action + no presented cert -> CERT_REQUIRED.
        - Privileged action + presented cert + thumbprint does not
          match -> CERT_NOT_APPROVED.
        - Privileged action + presented cert + thumbprint matches ->
          allowed.

        The thumbprint is compared case-insensitively on the hex
        digest because transports commonly uppercase / lowercase the
        encoding depending on the cipher suite.
        """
        if not policy.is_privileged(action):
            return
        if not presented_thumbprint:
            raise Forbidden(f"{SessionDenialReason.CERT_REQUIRED.value}: {action}")
        if (
            self.approved_client_cert_thumbprint is None
            or presented_thumbprint.strip().lower()
            != self.approved_client_cert_thumbprint.strip().lower()
        ):
            raise Forbidden(f"{SessionDenialReason.CERT_NOT_APPROVED.value}: {action}")


# ---------------------------------------------------------------------------
# Construction helper
# ---------------------------------------------------------------------------


def _coerce_datetime(value: Any) -> datetime:
    """Coerce an ``Any``-typed datetime boundary into a ``datetime``.

    Why: the domain's ``created_at`` / ``last_used_at`` / ``locked_until``
    fields are typed as ``Any`` so the persistence layer can stage them
    without coupling the domain to SQLAlchemy or Pydantic. The
    comparison operators mypy strict complains about when both sides
    are ``Any`` are silenced by this coercion.
    """
    if isinstance(value, datetime):
        return value
    raise InvariantViolation(f"expected datetime, got {type(value).__name__}")


def new_session(
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    now: Any,
) -> Session:
    """Factory for a freshly authenticated session.

    The new session is ``ACTIVE`` with a zero failure counter and a
    bumped ``last_used_at`` so the caller does not need a separate
    ``touch`` on the first request.
    """
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        state=SessionState.ACTIVE,
        created_at=now,
        last_used_at=now,
        failed_attempts=0,
        locked_until=None,
        approved_client_cert_thumbprint=None,
    )


__all__ = [
    "DEFAULT_ABSOLUTE_TIMEOUT",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_LOCKOUT_DURATION",
    "DEFAULT_MAX_FAILED_ATTEMPTS",
    "DEFAULT_PRIVILEGED_ACTIONS",
    "Session",
    "SessionDenialReason",
    "SessionPolicy",
    "SessionState",
    "new_session",
]
