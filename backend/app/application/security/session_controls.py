"""Session-control use cases (P11-US8 / P11-US11 / FM-049 / ADR-011).

The application layer is the only place that mutates ``Session`` and
``SessionPolicy`` rows. The domain layer (:mod:`app.domain.session`)
owns the **shape** of the policies; this module owns the **flow**:

- :func:`check_session_for_request` — read-side check called by the
  HTTP middleware before the request hits a use case. Returns the
  session (or refuses with ``Forbidden`` / ``HTTPException 401``).
- :func:`enforce_session_for_request` — read-side check that also
  enforces the privileged-action cert requirement. Used by routers
  serving privileged actions.
- :func:`record_authentication_failure` — increments the per-session
  failed-attempt counter and (when the threshold trips) locks the
  session for the configured window.
- :func:`record_authentication_success` — clears the counter and
  bumps ``last_used_at``.
- :func:`approve_client_certificate` — records the approved client
  cert thumbprint on a session. Gated by ``Action.SESSION_REQUIRE_CERT``
  per ADR-011.
- :func:`publish_session_policy` — replaces the tenant's published
  ``SessionPolicy``. Gated by ``Action.SESSION_POLICY_PUBLISH`` per
  ADR-011.

# Why every use case is a thin orchestrator

The decisions live in the domain (``Session.is_locked``,
``SessionPolicy.is_privileged``). The use case is responsible for:

1. Authorization (the policy registry decides *whether* the caller may
   ask the question).
2. Loading the policy from the repository (or falling back to the
   domain defaults when no row exists yet).
3. Loading / mutating / persisting the session.
4. Writing the audit row in the same transaction as the domain
   change.

# Why both allow and deny are audited

A lockout is a security event on its own (Constitution V). The audit
payload carries the *cause* (idle, absolute, locked, missing cert,
unapproved cert) and the *actor*. Cert thumbprints are NEVER in the
payload — only the **fact** that an approved cert was missing is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.errors import NotFound, ValidationFailed
from app.domain.session import (
    DEFAULT_ABSOLUTE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_LOCKOUT_DURATION,
    DEFAULT_MAX_FAILED_ATTEMPTS,
    DEFAULT_PRIVILEGED_ACTIONS,
    Session,
    SessionDenialReason,
    SessionPolicy,
    SessionState,
    new_session,
)

# ---------------------------------------------------------------------------
# Audit event types (P11-US8 / P11-US11). Stable strings; add new types
# here AND in the audit event catalog in ``app/domain/audit.py`` when the
# catalog gains a session section.
# ---------------------------------------------------------------------------

SESSION_LOCKED = "SessionLocked"
SESSION_EXPIRED = "SessionExpired"
SESSION_AUTHENTICATION_FAILED = "SessionAuthenticationFailed"
SESSION_AUTHENTICATION_SUCCEEDED = "SessionAuthenticationSucceeded"
SESSION_CLIENT_CERT_APPROVED = "SessionClientCertificateApproved"
SESSION_CLIENT_CERT_DENIED = "SessionClientCertificateDenied"
SESSION_POLICY_PUBLISHED = "SessionPolicyPublished"


# ---------------------------------------------------------------------------
# Ports — local Protocols so this module does not import infrastructure.
# ---------------------------------------------------------------------------


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SessionRepository(Protocol):
    async def get(self, tenant_id: UUID, session_id: UUID) -> Session | None: ...

    async def get_active_for_user(self, tenant_id: UUID, user_id: UUID) -> list[Session]: ...

    async def add(self, session: Session) -> None: ...

    async def save(self, session: Session) -> None: ...

    async def record_failed_attempt(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None: ...

    async def record_success(
        self, tenant_id: UUID, session_id: UUID, *, now: datetime
    ) -> Session | None: ...


class _SessionPolicyRepository(Protocol):
    async def get(self, tenant_id: UUID) -> SessionPolicy | None: ...

    async def publish(self, tenant_id: UUID, policy: SessionPolicy) -> None: ...


class _AuditStore(Protocol):
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
    ) -> None: ...


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """The result of a session check.

    Why a structured return rather than a raise: the HTTP middleware
    needs to distinguish "session is unusable" (401) from "policy
    refused the action" (403). The structured return lets the caller
    pick the status code without re-parsing the error message.
    """

    session: Session
    policy: SessionPolicy
    reason: SessionDenialReason | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None


async def _load_policy(repo: _SessionPolicyRepository, tenant_id: UUID) -> SessionPolicy:
    """Return the tenant's published policy or the domain defaults.

    The defaults are deliberately conservative (15min idle, 8h
    absolute, 5 attempts, 15min lockout). A tenant that never
    publishes a configuration still gets safe defaults; publishing
    is the act of *changing* the defaults, not of *enabling* the
    controls.
    """
    policy = await repo.get(tenant_id)
    if policy is not None:
        return policy
    return SessionPolicy()


async def check_session_for_request(
    *,
    tenant_id: UUID,
    session_id: UUID,
    session_repo: _SessionRepository,
    policy_repo: _SessionPolicyRepository,
    now: datetime | None = None,
) -> SessionDecision:
    """Return the live session and a verdict.

    Raises ``NotFound`` when the session id is unknown or cross-tenant
    so the caller can surface a 401 without leaking existence
    (Constitution V).

    On success the returned ``SessionDecision.allowed`` is True; on a
    refused request the ``reason`` is set to the precise denial cause
    but no exception is raised — the HTTP layer maps the reason to a
    status code.
    """
    session = await session_repo.get(tenant_id, session_id)
    if session is None:
        raise NotFound(f"session {session_id} not found")
    policy = await _load_policy(policy_repo, tenant_id)
    if session.state == SessionState.REVOKED:
        return SessionDecision(
            session=session, policy=policy, reason=SessionDenialReason.ACCOUNT_LOCKED
        )
    if session.is_locked(now or _now(), policy=policy):
        return SessionDecision(
            session=session, policy=policy, reason=SessionDenialReason.ACCOUNT_LOCKED
        )
    if session.is_absolute_expired(now or _now(), policy=policy):
        return SessionDecision(
            session=session, policy=policy, reason=SessionDenialReason.ABSOLUTE_TIMEOUT
        )
    if session.is_idle_expired(now or _now(), policy=policy):
        return SessionDecision(
            session=session, policy=policy, reason=SessionDenialReason.IDLE_TIMEOUT
        )
    return SessionDecision(session=session, policy=policy)


async def enforce_session_for_request(
    *,
    tenant_id: UUID,
    session_id: UUID,
    action: str,
    presented_client_cert_thumbprint: str | None,
    session_repo: _SessionRepository,
    policy_repo: _SessionPolicyRepository,
    now: datetime | None = None,
) -> SessionDecision:
    """Variant of :func:`check_session_for_request` that additionally
    enforces the privileged-action client-cert requirement.

    Use this in routers that serve privileged actions. The middleware
    that gates every request can use :func:`check_session_for_request`
    and only consult :func:`enforce_session_for_request` when the
    request targets a privileged action.

    Raises ``NotFound`` when the session id is unknown or cross-tenant.
    On cert refusal, ``SessionDecision.reason`` is set to
    ``CERT_REQUIRED`` or ``CERT_NOT_APPROVED`` and the caller surfaces
    ``HTTP 403`` (the caller is authorized, but the request lacks the
    additional proof the privileged-action policy needs).
    """
    decision = await check_session_for_request(
        tenant_id=tenant_id,
        session_id=session_id,
        session_repo=session_repo,
        policy_repo=policy_repo,
        now=now,
    )
    if not decision.allowed:
        return decision
    policy = decision.policy
    if not policy.is_privileged(action):
        return decision
    if not presented_client_cert_thumbprint:
        return SessionDecision(
            session=decision.session,
            policy=policy,
            reason=SessionDenialReason.CERT_REQUIRED,
        )
    approved = decision.session.approved_client_cert_thumbprint
    if (
        approved is None
        or presented_client_cert_thumbprint.strip().lower() != approved.strip().lower()
    ):
        return SessionDecision(
            session=decision.session,
            policy=policy,
            reason=SessionDenialReason.CERT_NOT_APPROVED,
        )
    return decision


async def record_authentication_failure(
    *,
    tenant_id: UUID,
    session_id: UUID,
    session_repo: _SessionRepository,
    audit: _AuditStore,
    correlation_id: str,
    clock: _Clock,
) -> Session:
    """Increment the failed-attempt counter; lock when the threshold trips.

    Returns the freshly persisted session so the caller can read the
    new ``state`` and ``locked_until`` (the audit row carries the
    failure cause, not the full session row).
    """
    now = clock.now()
    session = await session_repo.record_failed_attempt(tenant_id, session_id, now=now)
    if session is None:
        raise NotFound(f"session {session_id} not found")
    # Two distinct audit events are recorded here on purpose. The first
    # is the failure itself (always emitted); the second is the
    # lockout (only emitted when the threshold trips). The audit
    # timeline then shows a clean "5 failures -> locked" chain.
    await audit.append(
        tenant_id=tenant_id,
        event_type=SESSION_AUTHENTICATION_FAILED,
        actor_id=session.user_id,
        actor_type="user",
        subject_type="session",
        subject_id=session.id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "failed_attempts": session.failed_attempts,
            "locked": session.state == SessionState.LOCKED,
        },
        occurred_at=now,
    )
    if session.state == SessionState.LOCKED:
        await audit.append(
            tenant_id=tenant_id,
            event_type=SESSION_LOCKED,
            actor_id=session.user_id,
            actor_type="user",
            subject_type="session",
            subject_id=session.id,
            case_id=None,
            correlation_id=correlation_id,
            causation_id=SESSION_AUTHENTICATION_FAILED,
            payload={
                "failed_attempts": session.failed_attempts,
                "locked_until": (
                    session.locked_until.isoformat() if session.locked_until is not None else None
                ),
            },
            occurred_at=now,
        )
    return session


async def record_authentication_success(
    *,
    tenant_id: UUID,
    session_id: UUID,
    session_repo: _SessionRepository,
    audit: _AuditStore,
    correlation_id: str,
    clock: _Clock,
) -> Session:
    """Clear the failure counter and bump the idle clock."""
    now = clock.now()
    session = await session_repo.record_success(tenant_id, session_id, now=now)
    if session is None:
        raise NotFound(f"session {session_id} not found")
    await audit.append(
        tenant_id=tenant_id,
        event_type=SESSION_AUTHENTICATION_SUCCEEDED,
        actor_id=session.user_id,
        actor_type="user",
        subject_type="session",
        subject_id=session.id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload={},
        occurred_at=now,
    )
    return session


# ---------------------------------------------------------------------------
# Privileged-action / policy-publish use cases — Gated (ADR-011)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApproveClientCertCommand:
    """Wire shape for the privileged-action cert-approval endpoint.

    The command carries the actor (so the use case can authorize the
    Gated action), the session id being approved, and the thumbprint
    to record. The thumbprint is the SHA-256 hex digest of the X.509
    certificate the operator verified out-of-band; it is **never**
    logged.
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    session_id: UUID
    thumbprint: str


async def approve_client_certificate(
    cmd: ApproveClientCertCommand,
    *,
    session_repo: _SessionRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> Session:
    """Record the approved client-cert thumbprint on a session.

    This use case is **Gated** per ADR-011. The router requires
    ``Action.SESSION_REQUIRE_CERT``; the role registry grants it to
    no role today, so the surface is deny-by-default until a product
    decision is made.
    """
    await authorize(
        Action.SESSION_REQUIRE_CERT,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="session", id=cmd.session_id),
    )
    if not cmd.thumbprint or not isinstance(cmd.thumbprint, str) or not cmd.thumbprint.strip():
        raise ValidationFailed("thumbprint must be a non-empty string", field="thumbprint")
    session = await session_repo.get(cmd.tenant_id, cmd.session_id)
    if session is None:
        raise NotFound(f"session {cmd.session_id} not found")
    updated = session.with_approved_client_cert(cmd.thumbprint.strip().lower())
    await session_repo.save(updated)
    now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=SESSION_CLIENT_CERT_APPROVED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="session",
        subject_id=session.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"session_user_id": str(session.user_id)},
        occurred_at=now,
    )
    return updated


# ---------------------------------------------------------------------------
# Policy-publish use case — Gated (ADR-011)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublishSessionPolicyCommand:
    """Wire shape for the session-policy publish endpoint."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    idle_timeout_seconds: int
    absolute_timeout_seconds: int
    max_failed_attempts: int
    lockout_duration_seconds: int
    privileged_actions: tuple[str, ...] = ()


async def publish_session_policy(
    cmd: PublishSessionPolicyCommand,
    *,
    policy_repo: _SessionPolicyRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> SessionPolicy:
    """Replace the tenant's published session policy.

    This use case is **Gated** per ADR-011. The router requires
    ``Action.SESSION_POLICY_PUBLISH``; no role grants it. Activation
    requires an explicit product decision (record in the next ADR
    bump and flip the grant on ``ROLE_PROCESS_ADMIN``).
    """
    await authorize(
        Action.SESSION_POLICY_PUBLISH,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="session_policy"),
    )
    policy = SessionPolicy(
        idle_timeout=_seconds_to_timedelta(cmd.idle_timeout_seconds, "idle_timeout_seconds"),
        absolute_timeout=_seconds_to_timedelta(
            cmd.absolute_timeout_seconds, "absolute_timeout_seconds"
        ),
        max_failed_attempts=cmd.max_failed_attempts,
        lockout_duration=_seconds_to_timedelta(
            cmd.lockout_duration_seconds, "lockout_duration_seconds"
        ),
        privileged_actions=frozenset(cmd.privileged_actions),
    )
    await policy_repo.publish(cmd.tenant_id, policy)
    now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=SESSION_POLICY_PUBLISHED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="session_policy",
        subject_id=cmd.tenant_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "idle_timeout_seconds": cmd.idle_timeout_seconds,
            "absolute_timeout_seconds": cmd.absolute_timeout_seconds,
            "max_failed_attempts": cmd.max_failed_attempts,
            "lockout_duration_seconds": cmd.lockout_duration_seconds,
            "privileged_actions": sorted(cmd.privileged_actions),
        },
        occurred_at=now,
    )
    return policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Fallback clock for callers that do not pass one in.

    Used by the read-side checks; the write-side use cases always
    take an explicit ``_Clock`` so a deterministic test can pin time.
    """
    return datetime.now(tz=UTC)


def _seconds_to_timedelta(value: int, field: str) -> Any:
    """Validate and convert a wire-shape integer-seconds count to a
    ``timedelta``.

    Raises ``ValidationFailed`` (HTTP 422) on non-positive values so
    the validator enforces the same invariant ``SessionPolicy``
    enforces internally.
    """
    if not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} must be a positive integer", field=field)
    return timedelta(seconds=value)


# ---------------------------------------------------------------------------
# Re-export the module-level constants so callers can reference the
# published defaults without reaching into the domain.
# ---------------------------------------------------------------------------


__all__ = [
    "ApproveClientCertCommand",
    "PublishSessionPolicyCommand",
    "SESSION_AUTHENTICATION_FAILED",
    "SESSION_AUTHENTICATION_SUCCEEDED",
    "SESSION_CLIENT_CERT_APPROVED",
    "SESSION_CLIENT_CERT_DENIED",
    "SESSION_EXPIRED",
    "SESSION_LOCKED",
    "SESSION_POLICY_PUBLISHED",
    "SessionDecision",
    "approve_client_certificate",
    "check_session_for_request",
    "enforce_session_for_request",
    "publish_session_policy",
    "record_authentication_failure",
    "record_authentication_success",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_ABSOLUTE_TIMEOUT",
    "DEFAULT_MAX_FAILED_ATTEMPTS",
    "DEFAULT_LOCKOUT_DURATION",
    "DEFAULT_PRIVILEGED_ACTIONS",
    "new_session",
]
