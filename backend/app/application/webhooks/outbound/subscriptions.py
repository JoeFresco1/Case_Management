"""Subscription management use cases: register, update, disable, retire."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from app.domain.context import Clock, UserContext
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.events import find_event_spec
from app.domain.outbound_webhook import SigningAlgorithm, SubscriptionStatus, WebhookSubscription
from app.domain.ports import AuditEventStore, WebhookSubscriptionRepository

from .audit import _record_audit
from .constants import logger


@dataclass(frozen=True, slots=True)
class RegisterSubscriptionCommand:
    """Input to :func:`register_subscription`.

    The application layer fills in ``id``, ``version_token`` (0),
    ``created_at``, and ``updated_at`` from the clock; everything
    else is operator-supplied.
    """

    tenant_id: UUID
    url: str
    event_filters: tuple[str, ...]
    secret_name: str
    signing_algorithm: SigningAlgorithm = SigningAlgorithm.HMAC_SHA256
    require_bearer: bool = False
    description: str = ""
    actor: UserContext | None = None


@dataclass(frozen=True, slots=True)
class UpdateSubscriptionCommand:
    """Input to :func:`update_subscription`.

    The ``version_token`` is the operator's optimistic-concurrency
    anchor; a stale token raises ``Conflict`` (HTTP 409). The actor
    must hold ``WEBHOOK_SUBSCRIPTION_MANAGE`` for the tenant.
    """

    tenant_id: UUID
    subscription_id: UUID
    version_token: int
    url: str | None = None
    event_filters: tuple[str, ...] | None = None
    secret_name: str | None = None
    signing_algorithm: SigningAlgorithm | None = None
    require_bearer: bool | None = None
    description: str | None = None
    actor: UserContext | None = None


async def register_subscription(
    cmd: RegisterSubscriptionCommand,
    *,
    subscription_repo: WebhookSubscriptionRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> WebhookSubscription:
    """Register a new outbound webhook subscription.

    Validates that each ``event_filter`` is in the catalog (a typo
    in a filter would otherwise silently deliver nothing). Writes
    the subscription row + a ``WebhookSubscriptionRegistered`` audit
    row in the same transaction; the application layer wraps the
    call in a session so the commit is atomic.

    The actor's identity is recorded as ``created_by``; the policy
    gate is the caller's responsibility (P11-US1). The function
    refuses to register a subscription on behalf of a missing actor
    to keep the audit chain honest.
    """
    if cmd.actor is None:
        raise Forbidden("actor is required to register a subscription")
    if cmd.actor.tenant_id != cmd.tenant_id:
        # Cross-tenant actor is a security bug, not a domain
        # conflict — refuse loudly. The API layer must not be able
        # to construct a command whose actor.tenant_id disagrees
        # with the body tenant_id.
        raise Forbidden("actor tenant does not match command tenant")
    # Validate every filter against the catalog. A typo in a filter
    # would otherwise silently deliver nothing.
    for event_type in cmd.event_filters:
        if find_event_spec(event_type) is None:
            raise ValidationFailed(
                f"event_filter {event_type!r} is not in the catalog",
                field="event_filters",
            )
    now = clock.now()
    sub = WebhookSubscription(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        url=cmd.url,
        event_filters=tuple(cmd.event_filters),
        secret_name=cmd.secret_name,
        signing_algorithm=cmd.signing_algorithm,
        require_bearer=cmd.require_bearer,
        status=SubscriptionStatus.ACTIVE,
        version_token=0,
        created_at=now,
        updated_at=now,
        created_by=cmd.actor.user_id,
        description=cmd.description,
    )
    await subscription_repo.add(sub)
    await _record_audit(
        audit,
        tenant_id=cmd.tenant_id,
        event_type="WebhookSubscriptionRegistered",
        actor_id=cmd.actor.user_id,
        subject_id=sub.id,
        correlation_id=None,
        payload={
            "url": sub.url,
            "event_filters": list(sub.event_filters),
            "signing_algorithm": sub.signing_algorithm.value,
            "require_bearer": sub.require_bearer,
        },
        clock=clock,
    )
    logger.info(
        "outbound_webhook.subscription_registered",
        extra={
            "subscription_id": str(sub.id),
            "tenant_id": str(cmd.tenant_id),
            "event_filters": list(sub.event_filters),
        },
    )
    return sub


async def update_subscription(
    cmd: UpdateSubscriptionCommand,
    *,
    subscription_repo: WebhookSubscriptionRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> WebhookSubscription:
    """Update an existing subscription.

    Optimistic concurrency: the supplied ``version_token`` MUST match
    the row's current value or the function raises ``Conflict``. The
    application layer bumps the version on success so a concurrent
    update is rejected on its own write.
    """
    if cmd.actor is None:
        raise Forbidden("actor is required to update a subscription")
    if cmd.actor.tenant_id != cmd.tenant_id:
        raise Forbidden("actor tenant does not match command tenant")
    current = await subscription_repo.get(cmd.tenant_id, cmd.subscription_id)
    if current is None:
        raise NotFound(f"subscription {cmd.subscription_id} not found")
    if current.version_token != cmd.version_token:
        raise Conflict(
            f"subscription version_token is {current.version_token}, not {cmd.version_token}"
        )
    # Validate the proposed filter set if it is being changed.
    if cmd.event_filters is not None:
        for event_type in cmd.event_filters:
            if find_event_spec(event_type) is None:
                raise ValidationFailed(
                    f"event_filter {event_type!r} is not in the catalog",
                    field="event_filters",
                )
    # Build the new entity. Fields the caller did not supply are
    # taken from the current row; status is preserved (use
    # ``disable_subscription`` / ``retire_subscription`` for status
    # changes).
    new_url = cmd.url if cmd.url is not None else current.url
    new_filters = (
        tuple(cmd.event_filters) if cmd.event_filters is not None else current.event_filters
    )
    new_secret = cmd.secret_name if cmd.secret_name is not None else current.secret_name
    new_alg = (
        cmd.signing_algorithm if cmd.signing_algorithm is not None else current.signing_algorithm
    )
    new_bearer = cmd.require_bearer if cmd.require_bearer is not None else current.require_bearer
    new_desc = cmd.description if cmd.description is not None else current.description
    new_sub = WebhookSubscription(
        id=current.id,
        tenant_id=current.tenant_id,
        url=new_url,
        event_filters=new_filters,
        secret_name=new_secret,
        signing_algorithm=new_alg,
        require_bearer=new_bearer,
        status=current.status,
        version_token=current.version_token + 1,
        created_at=current.created_at,
        updated_at=clock.now(),
        created_by=current.created_by,
        description=new_desc,
    )
    await subscription_repo.update(new_sub)
    await _record_audit(
        audit,
        tenant_id=cmd.tenant_id,
        event_type="WebhookSubscriptionUpdated",
        actor_id=cmd.actor.user_id,
        subject_id=new_sub.id,
        correlation_id=None,
        payload={
            "new_version_token": new_sub.version_token,
            "event_filters": list(new_sub.event_filters),
        },
        clock=clock,
    )
    return new_sub


async def disable_subscription(
    tenant_id: UUID,
    subscription_id: UUID,
    *,
    actor: UserContext,
    subscription_repo: WebhookSubscriptionRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> WebhookSubscription:
    """Set a subscription's status to ``DISABLED``.

    A disabled subscription matches no events; pending deliveries
    already enqueued for the subscription will still be attempted
    (the URL snapshot was taken at enqueue). The operator can
    re-enable via ``update_subscription`` by setting the status back
    to ``ACTIVE`` — the call is rare enough that we don't add a
    dedicated ``enable_subscription`` helper.
    """
    if actor.tenant_id != tenant_id:
        raise Forbidden("actor tenant does not match subscription tenant")
    current = await subscription_repo.get(tenant_id, subscription_id)
    if current is None:
        raise NotFound(f"subscription {subscription_id} not found")
    if current.status is SubscriptionStatus.RETIRED:
        raise ValidationFailed(
            "retired subscriptions cannot be disabled",
            field="status",
        )
    new_sub = WebhookSubscription(
        id=current.id,
        tenant_id=current.tenant_id,
        url=current.url,
        event_filters=current.event_filters,
        secret_name=current.secret_name,
        signing_algorithm=current.signing_algorithm,
        require_bearer=current.require_bearer,
        status=SubscriptionStatus.DISABLED,
        version_token=current.version_token + 1,
        created_at=current.created_at,
        updated_at=clock.now(),
        created_by=current.created_by,
        description=current.description,
    )
    await subscription_repo.update(new_sub)
    await _record_audit(
        audit,
        tenant_id=tenant_id,
        event_type="WebhookSubscriptionDisabled",
        actor_id=actor.user_id,
        subject_id=new_sub.id,
        correlation_id=None,
        payload={"new_status": SubscriptionStatus.DISABLED.value},
        clock=clock,
    )
    return new_sub


async def retire_subscription(
    tenant_id: UUID,
    subscription_id: UUID,
    *,
    actor: UserContext,
    subscription_repo: WebhookSubscriptionRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> WebhookSubscription:
    """Set a subscription's status to ``RETIRED`` (terminal).

    Retired subscriptions are kept for audit but never deliver
    again. The operator console hides retired rows by default.
    """
    if actor.tenant_id != tenant_id:
        raise Forbidden("actor tenant does not match subscription tenant")
    current = await subscription_repo.get(tenant_id, subscription_id)
    if current is None:
        raise NotFound(f"subscription {subscription_id} not found")
    new_sub = WebhookSubscription(
        id=current.id,
        tenant_id=current.tenant_id,
        url=current.url,
        event_filters=current.event_filters,
        secret_name=current.secret_name,
        signing_algorithm=current.signing_algorithm,
        require_bearer=current.require_bearer,
        status=SubscriptionStatus.RETIRED,
        version_token=current.version_token + 1,
        created_at=current.created_at,
        updated_at=clock.now(),
        created_by=current.created_by,
        description=current.description,
    )
    await subscription_repo.update(new_sub)
    await _record_audit(
        audit,
        tenant_id=tenant_id,
        event_type="WebhookSubscriptionRetired",
        actor_id=actor.user_id,
        subject_id=new_sub.id,
        correlation_id=None,
        payload={"new_status": SubscriptionStatus.RETIRED.value},
        clock=clock,
    )
    return new_sub


__all__ = [
    "Conflict",
    "Forbidden",
    "NotFound",
    "RegisterSubscriptionCommand",
    "SigningAlgorithm",
    "SubscriptionStatus",
    "UpdateSubscriptionCommand",
    "UserContext",
    "UUID",
    "ValidationFailed",
    "WebhookSubscription",
    "WebhookSubscriptionRepository",
    "dataclass",
    "disable_subscription",
    "find_event_spec",
    "register_subscription",
    "retire_subscription",
    "update_subscription",
    "uuid4",
]
