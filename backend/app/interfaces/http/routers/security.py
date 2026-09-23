"""HTTP router for P11-US8 / P11-US11 session controls (FM-049 / ADR-011).

Endpoints:

- ``GET    /api/v1/security/sessions/{session_id}`` — read the live
  state of a session (idle, absolute, lockout, cert status). Uses the
  application-layer :func:`check_session_for_request`.
- ``POST   /api/v1/security/sessions/{session_id}/check-privilege`` —
  privileged-action pre-flight: returns 200 when the request carries
  an approved client cert, 403 when it does not. Uses
  :func:`enforce_session_for_request` so the cert check runs against
  the live session + policy.
- ``POST   /api/v1/security/sessions/{session_id}/approve-client-cert``
  — **Gated** per ADR-011; requires ``Action.SESSION_REQUIRE_CERT``.
- ``POST   /api/v1/security/authentication-failures`` — record a
  failed authentication attempt (increments the counter; locks the
  session when the threshold trips).
- ``POST   /api/v1/security/authentication-successes`` — record a
  successful authentication (clears the counter).
- ``POST   /api/v1/security/session-policy`` — **Gated** per ADR-011;
  requires ``Action.SESSION_POLICY_PUBLISH``.

# Authorization model

The router is the boundary where the application's session-control
use cases meet the HTTP transport. Every endpoint enforces:

1. Tenant scoping via the actor (``user.tenant_id``).
2. The session-control checks (idle / absolute / lockout) via the
   application use case.
3. For privileged actions, the client-cert check.
4. For Gated endpoints, the policy action (``SESSION_REQUIRE_CERT``
   / ``SESSION_POLICY_PUBLISH``).

The synthetic-environment marker is honored the same way as every
other router: ``SYNTHETIC_ENVIRONMENT=1`` is logged on startup and
visible in the UI; this router does not need its own marker.

# Why the failures / successes endpoints exist

The platform does not own authentication in this slice — the local
dev identity provider (``app.deps.LocalHeaderIdentityProvider``) and
the eventual Entra adapter both authenticate upstream. These
endpoints are the integration seam where the IdP adapter reports
authentication outcomes so the session-control counters stay in
sync. In a future slice the IdP itself will publish into the
session store; today the seam is HTTP for the local-dev flow.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.security import session_controls
from app.application.security.http_dependencies import SecurityHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import Forbidden, NotFound, ValidationFailed
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
)
from app.interfaces.http.dependencies import get_security_dependencies

# Break-glass cluster lives in a sibling module and is folded into this
# router's ``/security`` prefix via ``include_router`` below, keeping
# operation ids, tags, and paths identical to the pre-split layout.
from app.interfaces.http.routers import break_glass

# In-memory session/policy stores are defined in a sibling module and
# re-exported here: ``tests/security/test_session_controls.py`` (and any
# other caller) keeps importing them from this module unchanged. ``__all__``
# below marks them as intentional re-exports.
from app.interfaces.http.routers._session_stores import (
    _get_session_policy_store,
    _get_session_store,
    _InMemorySessionPolicyStore,
    _InMemorySessionStore,
)

# Synthetic stores are re-exported for callers that import them from this
# module (tests/security/test_session_controls.py); ``router`` is the
# FastAPI entry point consumed by ``app.main``.
__all__ = [
    "_InMemorySessionPolicyStore",
    "_InMemorySessionStore",
    "_get_session_policy_store",
    "_get_session_store",
    "router",
]

router = APIRouter(prefix="/security", tags=["security"])


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class SessionStatusResponse(BaseModel):
    """Wire shape for ``GET /security/sessions/{id}``."""

    session_id: UUID
    tenant_id: UUID
    user_id: UUID
    state: SessionState
    failed_attempts: int
    locked_until: datetime | None = None
    idle_expired: bool
    absolute_expired: bool
    approved_client_cert: bool
    last_used_at: datetime
    created_at: datetime


class PrivilegeCheckRequest(BaseModel):
    """Wire shape for ``POST /security/sessions/{id}/check-privilege``."""

    action: str = Field(min_length=1, max_length=128)
    client_cert_thumbprint: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "SHA-256 hex digest of the presented X.509 client "
            "certificate; absent when the request carries no cert."
        ),
    )


class PrivilegeCheckResponse(BaseModel):
    allowed: bool
    reason: SessionDenialReason | None = None
    action: str
    privileged: bool


class ApproveClientCertRequest(BaseModel):
    thumbprint: str = Field(
        min_length=1,
        max_length=128,
        description="SHA-256 hex digest of the approved X.509 cert.",
    )


class PublishSessionPolicyRequest(BaseModel):
    idle_timeout_seconds: int = Field(
        default=int(DEFAULT_IDLE_TIMEOUT.total_seconds()),
        gt=0,
        le=24 * 3600,
        description="Seconds of no activity before a session is logged out (max 24h).",
    )
    absolute_timeout_seconds: int = Field(
        default=int(DEFAULT_ABSOLUTE_TIMEOUT.total_seconds()),
        gt=0,
        le=7 * 24 * 3600,
        description="Maximum session lifetime in seconds (max 7 days).",
    )
    max_failed_attempts: int = Field(
        default=DEFAULT_MAX_FAILED_ATTEMPTS,
        gt=0,
        le=20,
        description="Consecutive authentication failures before lockout.",
    )
    lockout_duration_seconds: int = Field(
        default=int(DEFAULT_LOCKOUT_DURATION.total_seconds()),
        gt=0,
        le=24 * 3600,
        description="How long a locked session refuses access (max 24h).",
    )
    privileged_actions: tuple[str, ...] = Field(
        default=tuple(DEFAULT_PRIVILEGED_ACTIONS),
        description=(
            "Action names that require an approved client certificate. "
            "Empty by default — adding entries here activates the "
            "privileged-action check (Gated per ADR-011)."
        ),
    )


class SessionPolicyResponse(BaseModel):
    tenant_id: UUID
    idle_timeout_seconds: int
    absolute_timeout_seconds: int
    max_failed_attempts: int
    lockout_duration_seconds: int
    privileged_actions: list[str]


class SessionFailureRequest(BaseModel):
    """Wire shape for ``POST /security/authentication-failures`` and the
    matching success endpoint. The session id is the only payload field;
    the router reads the actor's tenant from the identity layer.
    """

    session_id: UUID


class SessionFailureResponse(BaseModel):
    session_id: UUID
    state: SessionState
    failed_attempts: int
    locked_until: datetime | None = None


# ---------------------------------------------------------------------------
# In-memory adapters live in ``_session_stores`` (synthetic-environment seam);
# they are re-exported above so existing imports keep resolving unchanged.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit adapter — resolved from the request-scoped security provider so
# the session-control events land on the same hash chain as everything
# else. The provider is supplied by the composition root
# (``app.state.security_http_dependencies``); endpoints that write
# audit resolve it via ``Depends(_get_security_dependencies)``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _to_status_response(
    session: Session, *, policy: SessionPolicy, now: datetime
) -> SessionStatusResponse:
    return SessionStatusResponse(
        session_id=session.id,
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        state=session.state,
        failed_attempts=session.failed_attempts,
        locked_until=session.locked_until,
        idle_expired=session.is_idle_expired(now, policy=policy),
        absolute_expired=session.is_absolute_expired(now, policy=policy),
        approved_client_cert=session.approved_client_cert_thumbprint is not None,
        last_used_at=session.last_used_at,
        created_at=session.created_at,
    )


def _to_policy_response(tenant_id: UUID, policy: SessionPolicy) -> SessionPolicyResponse:
    return SessionPolicyResponse(
        tenant_id=tenant_id,
        idle_timeout_seconds=int(policy.idle_timeout.total_seconds()),
        absolute_timeout_seconds=int(policy.absolute_timeout.total_seconds()),
        max_failed_attempts=policy.max_failed_attempts,
        lockout_duration_seconds=int(policy.lockout_duration.total_seconds()),
        privileged_actions=sorted(policy.privileged_actions),
    )


def _session_decision_to_http(detail: SessionDenialReason) -> HTTPException:
    """Map a session-control denial reason to the right HTTP status.

    Why 401 vs 403: idle / absolute / lockout are *authentication*
    failures — the caller's identity has lapsed. Missing / unapproved
    cert is *authorization* — the caller is authenticated but lacks
    the additional proof a privileged action requires.
    """
    if detail in {
        SessionDenialReason.IDLE_TIMEOUT,
        SessionDenialReason.ABSOLUTE_TIMEOUT,
        SessionDenialReason.ACCOUNT_LOCKED,
    }:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail.value,
        )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail.value,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}",
    response_model=SessionStatusResponse,
    summary="Read the live state of a session (idle / lockout / cert status).",
)
async def get_session_status(
    session_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),  # noqa: ARG001
    clock: Clock = Depends(get_clock),
) -> SessionStatusResponse:
    """Return the live state of ``session_id``.

    The endpoint requires ``AUDIT_READ`` so non-security callers cannot
    enumerate sessions. The application use case enforces the
    session-control checks (idle / absolute / lockout) and returns a
    structured ``SessionDecision``; the router maps the denial reason
    to ``HTTP 401`` or surfaces the live snapshot on success.
    """
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="session", id=session_id),
    )
    session_store = _get_session_store()
    policy_store = _get_session_policy_store()
    try:
        decision = await session_controls.check_session_for_request(
            tenant_id=user.tenant_id,
            session_id=session_id,
            session_repo=session_store,
            policy_repo=policy_store,
            now=clock.now(),
        )
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not decision.allowed:
        assert decision.reason is not None  # narrow type
        raise _session_decision_to_http(decision.reason)
    return _to_status_response(decision.session, policy=decision.policy, now=clock.now())


@router.post(
    "/sessions/{session_id}/check-privilege",
    response_model=PrivilegeCheckResponse,
    summary=(
        "Privileged-action pre-flight: 200 when the request carries an "
        "approved client cert, 403 when it does not."
    ),
)
async def check_privilege(
    body: PrivilegeCheckRequest,
    session_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
) -> PrivilegeCheckResponse:
    """Run the privileged-action pre-flight check.

    The endpoint returns ``HTTP 403`` when the request would be
    refused — mirroring the status a privileged action itself would
    surface so a UI can probe the policy without translating the
    structured response. Allowed requests return ``HTTP 200`` with the
    same shape so a UI can render the denial reason verbatim when it
    chose to call this endpoint and ignore the status code.
    """
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="session", id=session_id),
    )
    session_store = _get_session_store()
    policy_store = _get_session_policy_store()
    try:
        decision = await session_controls.enforce_session_for_request(
            tenant_id=user.tenant_id,
            session_id=session_id,
            action=body.action,
            presented_client_cert_thumbprint=body.client_cert_thumbprint,
            session_repo=session_store,
            policy_repo=policy_store,
            now=clock.now(),
        )
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    policy = decision.policy
    privileged = policy.is_privileged(body.action)
    if not decision.allowed:
        # Surface the denial via 403 (cert failure is an authorization
        # decision, not an authentication one; the caller is logged
        # in but lacks the additional proof). The body still carries
        # the structured response.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "allowed": False,
                "reason": decision.reason.value if decision.reason else None,
                "action": body.action,
                "privileged": privileged,
            },
        )
    return PrivilegeCheckResponse(
        allowed=True,
        reason=None,
        action=body.action,
        privileged=privileged,
    )


@router.post(
    "/sessions/{session_id}/approve-client-cert",
    response_model=SessionStatusResponse,
    summary=(
        "Record the approved client-cert thumbprint on a session. "
        "Gated per ADR-011 (Action.SESSION_REQUIRE_CERT); no role grants "
        "this action today."
    ),
)
async def approve_client_cert(
    body: ApproveClientCertRequest,
    session_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> SessionStatusResponse:
    """Approve a client certificate for privileged actions on ``session_id``.

    This endpoint is the privileged-access-workstation seam (P11-US11).
    Activation requires an explicit product decision (ADR-011 Gated):
    flip the grant on ``ROLE_PROCESS_ADMIN`` to expose the surface.
    """
    session_store = _get_session_store()
    policy_store = _get_session_policy_store()
    audit = dependencies.for_session(db).audit
    try:
        session = await session_controls.approve_client_certificate(
            session_controls.ApproveClientCertCommand(
                tenant_id=user.tenant_id,
                actor=user,
                correlation_id=correlation.correlation_id,
                session_id=session_id,
                thumbprint=body.thumbprint,
            ),
            session_repo=session_store,
            audit=audit,
            clock=clock,
        )
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if db is not None:
        await db.commit()
    policy = await session_controls_check_policy(policy_store, user.tenant_id)
    return _to_status_response(session, policy=policy, now=clock.now())


async def session_controls_check_policy(
    policy_store: _InMemorySessionPolicyStore, tenant_id: UUID
) -> SessionPolicy:
    policy = await policy_store.get(tenant_id)
    return policy if policy is not None else SessionPolicy()


@router.post(
    "/authentication-failures",
    response_model=SessionFailureResponse,
    status_code=status.HTTP_200_OK,
    summary="Record a failed authentication attempt for ``session_id``.",
)
async def record_authentication_failure(
    body: SessionFailureRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> SessionFailureResponse:
    """Increment the per-session failure counter.

    Why this is a public endpoint: the IdP adapter publishes
    authentication outcomes via this seam. The endpoint requires
    ``AUDIT_READ`` (the same shape the audit report does) so only
    authorized callers — i.e. the IdP adapter or a process
    administrator replaying the synthetic seed — can submit failures.
    """
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="session", id=body.session_id),
    )
    session_store = _get_session_store()
    audit = dependencies.for_session(db).audit
    try:
        session = await session_controls.record_authentication_failure(
            tenant_id=user.tenant_id,
            session_id=body.session_id,
            session_repo=session_store,
            audit=audit,
            correlation_id=correlation.correlation_id,
            clock=clock,
        )
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if db is not None:
        await db.commit()
    return SessionFailureResponse(
        session_id=session.id,
        state=session.state,
        failed_attempts=session.failed_attempts,
        locked_until=session.locked_until,
    )


@router.post(
    "/authentication-successes",
    response_model=SessionFailureResponse,
    status_code=status.HTTP_200_OK,
    summary="Record a successful authentication for ``session_id``.",
)
async def record_authentication_success(
    body: SessionFailureRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> SessionFailureResponse:
    """Clear the per-session failure counter and bump the idle clock.

    The IdP adapter publishes successful authentication outcomes via
    this seam. The endpoint requires ``AUDIT_READ``; activation is the
    same as ``/authentication-failures``.
    """
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="session", id=body.session_id),
    )
    session_store = _get_session_store()
    audit = dependencies.for_session(db).audit
    try:
        session = await session_controls.record_authentication_success(
            tenant_id=user.tenant_id,
            session_id=body.session_id,
            session_repo=session_store,
            audit=audit,
            correlation_id=correlation.correlation_id,
            clock=clock,
        )
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if db is not None:
        await db.commit()
    return SessionFailureResponse(
        session_id=session.id,
        state=session.state,
        failed_attempts=session.failed_attempts,
        locked_until=session.locked_until,
    )


@router.post(
    "/session-policy",
    response_model=SessionPolicyResponse,
    status_code=status.HTTP_200_OK,
    summary=(
        "Replace the tenant's published session policy. "
        "Gated per ADR-011 (Action.SESSION_POLICY_PUBLISH); no role grants "
        "this action today."
    ),
)
async def publish_session_policy(
    body: PublishSessionPolicyRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> SessionPolicyResponse:
    """Publish the tenant's session policy (idle / lockout / privileged actions).

    Gated per ADR-011: the endpoint requires
    ``Action.SESSION_POLICY_PUBLISH``; no role grants it. Activation
    requires an explicit product decision.
    """
    policy_store = _get_session_policy_store()
    audit = dependencies.for_session(db).audit
    try:
        policy = await session_controls.publish_session_policy(
            session_controls.PublishSessionPolicyCommand(
                tenant_id=user.tenant_id,
                actor=user,
                correlation_id=correlation.correlation_id,
                idle_timeout_seconds=body.idle_timeout_seconds,
                absolute_timeout_seconds=body.absolute_timeout_seconds,
                max_failed_attempts=body.max_failed_attempts,
                lockout_duration_seconds=body.lockout_duration_seconds,
                privileged_actions=tuple(body.privileged_actions),
            ),
            policy_repo=policy_store,
            audit=audit,
            clock=clock,
        )
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if db is not None:
        await db.commit()
    return _to_policy_response(user.tenant_id, policy)


@router.get(
    "/session-policy",
    response_model=SessionPolicyResponse,
    summary="Read the tenant's published session policy (defaults if none).",
)
async def get_session_policy(
    user: UserContext = Depends(get_current_user),
) -> SessionPolicyResponse:
    """Return the tenant's published policy (defaults if none).

    Requires ``AUDIT_READ`` so non-administrators cannot enumerate the
    tenant's lockout configuration.
    """
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="session_policy"),
    )
    policy_store = _get_session_policy_store()
    policy = await policy_store.get(user.tenant_id)
    if policy is None:
        policy = SessionPolicy()
    return _to_policy_response(user.tenant_id, policy)


# Fold the break-glass cluster in after the session routes so path order
# and OpenAPI operation order match the pre-split single-file layout.
# Child router declares no prefix/tags — they are applied by include_router.
router.include_router(break_glass.router)

__all__ = ["router"]
