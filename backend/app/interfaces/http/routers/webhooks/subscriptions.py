"""Outbound webhook subscription management routes (FM-056 / P13-US10 / ADR-013).

The OUTBOUND half of the webhook story. The two surfaces share the
``/api/v1/webhooks`` URL prefix but are operationally distinct:

- Inbound:  ``POST /api/v1/webhooks/{channel}`` — platform is the
            consumer; HMAC verifies the producer; idempotency anchor
            is the producer's ``X-Webhook-Id`` (:mod:`receive`).
- Outbound: ``POST /api/v1/webhooks/subscriptions`` — platform is
            the producer; HMAC signs for the subscriber; idempotency
            anchor is the platform's own ``event_id`` (this module).

See 01-deduplicated-feature-map.md:189 and ADR-007 §"Inbound vs
Outbound" for the rationale. Every endpoint here (list, create,
update, disable, retire, replay — in that registration order) rides
the fail-closed :func:`_authorize_subscription_manage` gate (deny by
default).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.webhooks import outbound as outbound_uc
from app.application.webhooks.http_dependencies import WebhookHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import ValidationFailed

# OutboundDelivery + DeliveryState are the import path for the replay
# handler below; they stay at the top of the module to keep the
# import surface obvious.
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    WebhookSubscription,
)
from app.interfaces.http.dependencies import get_webhook_dependencies
from app.interfaces.http.pagination import DEFAULT_LIMIT, MAX_LIMIT

from .core import router
from .schemas import (
    SubscriptionCreateRequest,
    SubscriptionListResponse,
    SubscriptionResponse,
    SubscriptionUpdateRequest,
)

# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------


async def _authorize_subscription_manage(user: UserContext) -> None:
    """Enforce the subscription-manage right (fail-closed, deny by default).

    Why ``ADMIN_TENANT_CONFIG`` and not a dedicated action: the
    authorization vocabulary (``app.application.authorization.Action``)
    has no ``WEBHOOK_SUBSCRIPTION_MANAGE`` member — the outbound
    module docstring names it aspirationally, but the live policy
    registry is the authority. Subscription management is operator
    configuration, so it rides the same gate as extension
    registration, outbox replay, and system settings. A worker with
    only ``intake``/``reviewer`` roles is denied with 403.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="webhook_subscription"),
    )


def _require_idempotency_key(idempotency_key: str | None) -> None:
    """Raise 400 when a mutating subscription endpoint lacks the key."""
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )


def _subscription_to_response(
    sub: WebhookSubscription, correlation_id: str
) -> SubscriptionResponse:
    """Map a domain ``WebhookSubscription`` onto the subscription wire shape."""
    return SubscriptionResponse(
        id=str(sub.id),
        tenant_id=str(sub.tenant_id),
        url=sub.url,
        event_filters=list(sub.event_filters),
        secret_name=sub.secret_name,
        signing_algorithm=sub.signing_algorithm.value,
        require_bearer=sub.require_bearer,
        status=sub.status.value,
        version_token=sub.version_token,
        description=sub.description,
        created_at=sub.created_at.isoformat() if sub.created_at else "",
        updated_at=sub.updated_at.isoformat() if sub.updated_at else "",
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


@router.get(
    "/webhooks/subscriptions",
    response_model=SubscriptionListResponse,
    summary="List outbound webhook subscriptions (paginated, tenant-scoped)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
    },
)
async def list_subscriptions(
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum rows in the page (default 50, max 200)",
    ),
    offset: int = Query(default=0, ge=0, description="Zero-based offset into newest-first order"),
) -> SubscriptionListResponse:
    """List the caller's tenant subscriptions newest-first.

    Tenant scope is enforced by the repository filter
    (``WHERE tenant_id = :tenant_id``); a cross-tenant id is never
    observable. ``next_cursor`` carries the next ``offset`` as a
    string, or ``None`` when the window exhausts the collection.
    """
    await _authorize_subscription_manage(user)
    repos = dependencies.for_session(db)
    rows = await repos.subscription.list(user.tenant_id, limit=limit + 1, offset=offset)
    page_rows = list(rows[:limit])
    next_cursor = str(offset + limit) if len(rows) > limit else None
    return SubscriptionListResponse(
        items=[_subscription_to_response(r, correlation.correlation_id) for r in page_rows],
        next_cursor=next_cursor,
        limit=limit,
    )


@router.post(
    "/webhooks/subscriptions",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new outbound webhook subscription",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Caller is not authenticated"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid subscription payload (unknown event_filter, bad URL, etc.)"
        },
    },
)
async def create_subscription(
    body: SubscriptionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
) -> SubscriptionResponse:
    """Register a new outbound webhook subscription.

    Headers: ``Idempotency-Key`` is required (AGENTS.md §10). The
    subscription is created in the same transaction as the
    ``WebhookSubscriptionRegistered`` audit row so a partial commit
    never leaves a subscription without provenance.
    """
    _require_idempotency_key(idempotency_key)
    await _authorize_subscription_manage(user)
    try:
        alg = outbound_uc.SigningAlgorithm(body.signing_algorithm)
    except ValueError as exc:
        raise ValidationFailed(
            f"unknown signing_algorithm: {body.signing_algorithm!r}",
            field="signing_algorithm",
        ) from exc

    repos = dependencies.for_session(db)
    sub_repo = repos.subscription
    audit_store = repos.audit
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=user.tenant_id,
        url=body.url,
        event_filters=tuple(body.event_filters),
        secret_name=body.secret_name,
        signing_algorithm=alg,
        require_bearer=body.require_bearer,
        description=body.description,
        actor=user,
    )
    sub = await outbound_uc.register_subscription(
        cmd,
        subscription_repo=sub_repo,
        audit=audit_store,
        clock=clock,
    )
    await db.commit()
    return _subscription_to_response(sub, correlation.correlation_id)


@router.patch(
    "/webhooks/subscriptions/{subscription_id}",
    response_model=SubscriptionResponse,
    summary="Update an existing outbound webhook subscription",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Caller is not authenticated"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
        status.HTTP_404_NOT_FOUND: {"description": "Subscription not found in tenant"},
        status.HTTP_409_CONFLICT: {"description": "Stale version_token"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid payload"},
    },
)
async def update_subscription(
    subscription_id: UUID,
    body: SubscriptionUpdateRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> SubscriptionResponse:
    """Update a subscription with optimistic concurrency.

    ``If-Match: <version_token>`` is the canonical concurrency
    anchor (AGENTS.md §10). The body ``version_token`` is accepted
    for back-compat; when both are present they MUST agree or the
    update is rejected with 409 ``conflict``. When only the header
    is present it is the anchor.
    """
    await _authorize_subscription_manage(user)
    effective_token = body.version_token
    if if_match is not None:
        try:
            header_token = int(if_match.strip().strip('"'))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_if_match",
                    "message": "If-Match header must be an integer version_token",
                },
            ) from exc
        if header_token != body.version_token:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "conflict",
                    "message": (
                        "If-Match header does not match body version_token; "
                        "refetch the subscription and retry"
                    ),
                },
            )
        effective_token = header_token
    alg: outbound_uc.SigningAlgorithm | None = None
    if body.signing_algorithm is not None:
        try:
            alg = outbound_uc.SigningAlgorithm(body.signing_algorithm)
        except ValueError as exc:
            raise ValidationFailed(
                f"unknown signing_algorithm: {body.signing_algorithm!r}",
                field="signing_algorithm",
            ) from exc

    repos = dependencies.for_session(db)
    sub_repo = repos.subscription
    audit_store = repos.audit
    cmd = outbound_uc.UpdateSubscriptionCommand(
        tenant_id=user.tenant_id,
        subscription_id=subscription_id,
        version_token=effective_token,
        url=body.url,
        event_filters=tuple(body.event_filters) if body.event_filters is not None else None,
        secret_name=body.secret_name,
        signing_algorithm=alg,
        require_bearer=body.require_bearer,
        description=body.description,
        actor=user,
    )
    sub = await outbound_uc.update_subscription(
        cmd,
        subscription_repo=sub_repo,
        audit=audit_store,
        clock=clock,
    )
    await db.commit()
    return _subscription_to_response(sub, correlation.correlation_id)


# ---------------------------------------------------------------------------
# Subscription lifecycle actions (disable / retire / replay)
# ---------------------------------------------------------------------------


@router.post(
    "/webhooks/subscriptions/{subscription_id}/disable",
    response_model=SubscriptionResponse,
    summary="Disable an outbound webhook subscription",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Caller is not authenticated"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
        status.HTTP_404_NOT_FOUND: {"description": "Subscription not found in tenant"},
    },
)
async def disable_subscription(
    subscription_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubscriptionResponse:
    """Disable a subscription (it stops matching new events)."""
    _require_idempotency_key(idempotency_key)
    await _authorize_subscription_manage(user)
    repos = dependencies.for_session(db)
    sub_repo = repos.subscription
    audit_store = repos.audit
    sub = await outbound_uc.disable_subscription(
        user.tenant_id,
        subscription_id,
        actor=user,
        subscription_repo=sub_repo,
        audit=audit_store,
        clock=clock,
    )
    await db.commit()
    return _subscription_to_response(sub, correlation.correlation_id)


@router.post(
    "/webhooks/subscriptions/{subscription_id}/retire",
    response_model=SubscriptionResponse,
    summary="Retire an outbound webhook subscription (terminal)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Caller is not authenticated"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
        status.HTTP_404_NOT_FOUND: {"description": "Subscription not found in tenant"},
    },
)
async def retire_subscription(
    subscription_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubscriptionResponse:
    """Retire a subscription (terminal; no further deliveries)."""
    _require_idempotency_key(idempotency_key)
    await _authorize_subscription_manage(user)
    repos = dependencies.for_session(db)
    sub_repo = repos.subscription
    audit_store = repos.audit
    sub = await outbound_uc.retire_subscription(
        user.tenant_id,
        subscription_id,
        actor=user,
        subscription_repo=sub_repo,
        audit=audit_store,
        clock=clock,
    )
    await db.commit()
    return _subscription_to_response(sub, correlation.correlation_id)


@router.post(
    "/webhooks/subscriptions/{subscription_id}/replay",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replay all dead-lettered deliveries for a subscription",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Caller is not authenticated"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller cannot manage subscriptions"},
        status.HTTP_404_NOT_FOUND: {"description": "Subscription not found in tenant"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Webhook delivery dispatcher is not configured"
        },
    },
)
async def replay_subscription(
    subscription_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: WebhookHttpDependencyProvider = Depends(get_webhook_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubscriptionResponse:
    """Reset all dead-lettered deliveries for a subscription to PENDING.

    This is the operator's escape hatch when a subscriber's endpoint
    was down for an extended period: the platform dead-letters
    deliveries that exhaust the cap; the operator fixes the
    endpoint and replays. The replay resets ``attempt_count`` to
    zero and state to ``PENDING``; the next dispatcher tick will
    re-attempt.

    The response returns the subscription (the same shape every
    other subscription endpoint returns) so a UI can refresh its
    detail panel from the response.

    ``Idempotency-Key`` is required; a retried replay with the same
    key is a safe no-op at the HTTP layer (the middleware replays
    the prior 202). Replaying a subscription with no dead-lettered
    deliveries succeeds with zero resets (idempotent).
    """
    _require_idempotency_key(idempotency_key)
    await _authorize_subscription_manage(user)
    repos = dependencies.for_session(db)
    sub_repo = repos.subscription
    delivery_repo = repos.delivery
    audit_store = repos.audit

    sub = await sub_repo.get(user.tenant_id, subscription_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "subscription_not_found",
                "message": f"subscription {subscription_id} not found in tenant",
            },
        )
    # Reset all dead-lettered rows. The list is bounded by the
    # operator's expectation (50 is a reasonable default for a
    # single replay call).
    dead = await delivery_repo.list_dead_lettered(user.tenant_id, limit=200)
    for row in dead:
        if row.subscription_id != subscription_id:
            continue
        reset = OutboundDelivery(
            id=row.id,
            tenant_id=row.tenant_id,
            subscription_id=row.subscription_id,
            event_id=row.event_id,
            event_type=row.event_type,
            payload_json=row.payload_json,
            correlation_id=row.correlation_id,
            occurred_at=row.occurred_at,
            target_url=row.target_url,
            state=DeliveryState.PENDING,
            attempt_count=0,
            last_status_code=None,
            last_error=None,
            first_attempted_at=None,
            last_attempted_at=None,
            succeeded_at=None,
            created_at=row.created_at,
        )
        await delivery_repo.update(reset)
    await audit_store.append(
        tenant_id=user.tenant_id,
        event_type="OutboundDeliveryReplayRequested",
        actor_id=user.user_id,
        actor_type="user",
        subject_type="WebhookSubscription",
        subject_id=subscription_id,
        case_id=None,
        correlation_id=correlation.correlation_id,
        causation_id=None,
        payload={
            "dead_lettered_count": sum(1 for r in dead if r.subscription_id == subscription_id)
        },
        occurred_at=clock.now(),
    )
    await db.commit()
    return _subscription_to_response(sub, correlation.correlation_id)
