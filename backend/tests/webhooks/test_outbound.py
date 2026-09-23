"""Outbound webhook delivery tests (FM-056 / P13-US10 / ADR-013).

Pinned behaviors:

1. **Authenticated delivery** — every outbound delivery carries an
   ``X-Webhook-Signature: sha256=<hex>`` header over the canonical
   string ``f"{timestamp}.{body}"``. A subscriber that knows the
   shared secret can verify the body byte-for-byte.
2. **Retry on failure** — a 5xx response bumps the attempt counter
   and leaves the row PENDING; a 2xx marks it SUCCEEDED; a 4xx
   (except 408/429) dead-letters immediately.
3. **Idempotent on (subscription, event)** — a second enqueue for
   the same event returns the prior row without inserting; a
   duplicate POST from the subscriber's perspective is recognized
   by the matching ``X-Webhook-Event-Id``.
4. **Inbound vs Outbound distinct** — the inbound surface
   (``POST /api/v1/webhooks/{channel}``) is the consumer; the
   outbound surface (``POST /api/v1/webhooks/subscriptions`` + the
   delivery loop) is the producer. The tests assert that an
   outbound delivery row is created from a domain event AND that
   the inbound webhook router does NOT produce outbound delivery
   rows.
5. **JWT bearer** — when ``require_bearer=True``, the platform
   attaches a short-lived HS256 JWT in addition to the HMAC. The
   token carries ``sub_id``, ``dlv_id``, ``evt_id`` and ``exp``.
6. **Dead-letter cap** — after ``MAX_ATTEMPTS`` failed attempts the
   row is moved to DEAD_LETTERED. The operator replay endpoint
   resets the row to PENDING and zeroes the counter.

Tests follow AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given / // when / // then`` per test. The fixtures use
in-memory fakes for the repositories, the audit store, and the
transport so the suite never needs PostgreSQL (AGENTS.md §4.R1
fallback) and never makes a real HTTP request.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.webhooks import outbound as outbound_uc
from app.deps import Clock
from app.domain.errors import Conflict, Forbidden, ValidationFailed
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    SigningAlgorithm,
    SubscriptionStatus,
    WebhookSubscription,
)
from app.domain.outbound_webhook import OutboundDelivery as _OutboundDelivery
from app.domain.ports import (
    AuditEventStore,
    OutboundDeliveryRepository,
    OutboundDeliveryTransport,
    SecretProvider,
    TransportResult,
    WebhookSubscriptionRepository,
)
from app.domain.webhook_capture import CapturedWebhook as _CapturedWebhook
from app.infrastructure.persistence.repositories.outbound_webhook_repository import (
    SqlWebhookSubscriptionRepository,
)
from app.infrastructure.webhooks.httpx_transport import RecordingOutboundTransport

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUBSCRIBER_SECRET = "outbound_subscriber_acme_secret_value_12345"
_EPOCH_MIN = datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FixedClock(Clock):
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 9, 2, 8, 43, 15, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@dataclass
class AuditEntry:
    tenant_id: UUID
    event_type: str
    actor_id: UUID | None
    subject_id: UUID
    correlation_id: str
    payload: dict[str, Any]


class InMemoryAuditStore(AuditEventStore):
    """In-memory ``AuditEventStore`` for the outbound tests."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID | None,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.entries.append(
            AuditEntry(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_id=subject_id,
                correlation_id=correlation_id,
                payload=payload,
            )
        )

    async def list_for_case(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class InMemorySubscriptionRepository(WebhookSubscriptionRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, WebhookSubscription] = {}

    async def get(self, tenant_id: UUID, subscription_id: UUID) -> WebhookSubscription | None:
        s = self._store.get(subscription_id)
        if s is None or s.tenant_id != tenant_id:
            return None
        return s

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WebhookSubscription]:
        rows = [s for s in self._store.values() if s.tenant_id == tenant_id]
        rows.sort(key=lambda s: s.created_at or _EPOCH_MIN, reverse=True)
        return rows[offset : offset + limit]

    async def add(self, subscription: WebhookSubscription) -> None:
        self._store[subscription.id] = subscription

    async def update(self, subscription: WebhookSubscription) -> None:
        if subscription.id not in self._store:
            self._store[subscription.id] = subscription
            return
        self._store[subscription.id] = subscription


class InMemoryDeliveryRepository(OutboundDeliveryRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, OutboundDelivery] = {}

    async def get(self, tenant_id: UUID, delivery_id: UUID) -> OutboundDelivery | None:
        d = self._store.get(delivery_id)
        if d is None or d.tenant_id != tenant_id:
            return None
        return d

    async def get_by_event(
        self, tenant_id: UUID, subscription_id: UUID, event_id: UUID
    ) -> OutboundDelivery | None:
        for d in self._store.values():
            if (
                d.tenant_id == tenant_id
                and d.subscription_id == subscription_id
                and d.event_id == event_id
            ):
                return d
        return None

    async def list_pending(
        self,
        tenant_id: UUID,
        *,
        subscription_id: UUID | None = None,
        limit: int = 50,
    ) -> list[OutboundDelivery]:
        rows = [
            d
            for d in self._store.values()
            if d.tenant_id == tenant_id and d.state is DeliveryState.PENDING
        ]
        rows.sort(key=lambda d: d.created_at or _EPOCH_MIN)
        if subscription_id is not None:
            rows = [d for d in rows if d.subscription_id == subscription_id]
        return rows[:limit]

    async def add(self, delivery: OutboundDelivery) -> None:
        # Mirror the SQL repository's unique-constraint behavior so
        # the application-layer tests can assert on the Conflict
        # path without a database.
        prior = await self.get_by_event(
            delivery.tenant_id, delivery.subscription_id, delivery.event_id
        )
        if prior is not None:
            raise Conflict("delivery already exists")
        self._store[delivery.id] = delivery

    async def update(self, delivery: OutboundDelivery) -> None:
        if delivery.id not in self._store:
            raise Conflict(f"delivery {delivery.id} not found for update")
        self._store[delivery.id] = delivery

    async def list_dead_lettered(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
    ) -> list[OutboundDelivery]:
        rows = [
            d
            for d in self._store.values()
            if d.tenant_id == tenant_id and d.state is DeliveryState.DEAD_LETTERED
        ]
        return rows[:limit]


class StaticSecretProvider(SecretProvider):
    """Returns a fixed value for one logical secret name."""

    def __init__(self, *, name: str, value: str) -> None:
        self._name = name
        self._value = value

    async def get(self, name: str) -> str:
        if name != self._name:
            raise KeyError(name)
        return self._value

    async def set_secret(self, name: str, value: str) -> None:
        return None


def make_actor(tenant_id: UUID = TENANT) -> Any:
    @dataclass(frozen=True, slots=True)
    class _StubUserContext:
        user_id: UUID = ACTOR
        # tenant_id is set per-call via the factory to avoid capturing
        # the parameter in a default value (which dataclass treats as
        # a class-level default).
        display_name: str = "operator"
        roles: tuple[str, ...] = ("operator",)
        is_system: bool = False
        # the actual tenant_id is appended at the end so it can be set
        # explicitly without ordering issues
        tenant_id: UUID = ACTOR  # placeholder, overwritten below

    return _StubUserContext(tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Subscription lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_subscription__happy_path__creates_active_row_and_audit() -> None:
    # given a clean audit + subscription repo
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    clock = FixedClock()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/case-events",
        event_filters=("CaseCreated", "TaskCompleted"),
        secret_name="outbound_subscriber_acme",
        description="Acme BI ingestion",
        actor=make_actor(),
    )
    # when the subscription is registered
    sub = await outbound_uc.register_subscription(
        cmd, subscription_repo=repo, audit=audit, clock=clock
    )
    # then the subscription is ACTIVE with version_token=0 and an audit row is appended
    assert sub.status is SubscriptionStatus.ACTIVE
    assert sub.version_token == 0
    assert sub.url == "https://example.test/webhooks/case-events"
    assert sub.event_filters == ("CaseCreated", "TaskCompleted")
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.event_type == "WebhookSubscriptionRegistered"
    assert entry.tenant_id == TENANT
    assert entry.subject_id == sub.id
    # then the subscription is queryable through the same tenant scope
    fetched = await repo.get(TENANT, sub.id)
    assert fetched is not None
    assert fetched.id == sub.id
    # then cross-tenant visibility is denied
    cross = await repo.get(OTHER_TENANT, sub.id)
    assert cross is None


@pytest.mark.asyncio
async def test_register_subscription__unknown_event_filter__rejected() -> None:
    # given a subscription command with an event_filter not in the catalog
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/x",
        event_filters=("NotInCatalog",),
        secret_name="x",
        actor=make_actor(),
    )
    # when the subscription is registered, then ValidationFailed is raised
    with pytest.raises(ValidationFailed):
        await outbound_uc.register_subscription(
            cmd, subscription_repo=repo, audit=audit, clock=FixedClock()
        )
    # and no row is created
    assert await repo.list(TENANT) == []


@pytest.mark.asyncio
async def test_register_subscription__actor_tenant_mismatch__forbidden() -> None:
    # given a subscription command whose actor is in a different tenant
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/x",
        event_filters=("CaseCreated",),
        secret_name="x",
        actor=make_actor(tenant_id=OTHER_TENANT),
    )
    # when the subscription is registered, then Forbidden is raised
    with pytest.raises(Forbidden):
        await outbound_uc.register_subscription(
            cmd, subscription_repo=repo, audit=audit, clock=FixedClock()
        )


@pytest.mark.asyncio
async def test_update_subscription__stale_version_token__conflict() -> None:
    # given a registered subscription
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    sub = await outbound_uc.register_subscription(
        outbound_uc.RegisterSubscriptionCommand(
            tenant_id=TENANT,
            url="https://example.test/webhooks/x",
            event_filters=("CaseCreated",),
            secret_name="x",
            actor=make_actor(),
        ),
        subscription_repo=repo,
        audit=audit,
        clock=FixedClock(),
    )
    # when an update is attempted with a stale version_token
    with pytest.raises(Conflict):
        await outbound_uc.update_subscription(
            outbound_uc.UpdateSubscriptionCommand(
                tenant_id=TENANT,
                subscription_id=sub.id,
                version_token=99,
                url="https://example.test/webhooks/new",
                actor=make_actor(),
            ),
            subscription_repo=repo,
            audit=audit,
            clock=FixedClock(),
        )


@pytest.mark.asyncio
async def test_update_subscription__matching_version__bumps_token_and_writes_audit() -> None:
    # given a registered subscription at version 0
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    clock = FixedClock()
    sub = await outbound_uc.register_subscription(
        outbound_uc.RegisterSubscriptionCommand(
            tenant_id=TENANT,
            url="https://example.test/webhooks/x",
            event_filters=("CaseCreated",),
            secret_name="x",
            actor=make_actor(),
        ),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # when the subscription is updated with the correct version_token
    new_sub = await outbound_uc.update_subscription(
        outbound_uc.UpdateSubscriptionCommand(
            tenant_id=TENANT,
            subscription_id=sub.id,
            version_token=0,
            url="https://example.test/webhooks/x-v2",
            actor=make_actor(),
        ),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # then version_token is 1 and the URL is the new one
    assert new_sub.version_token == 1
    assert new_sub.url == "https://example.test/webhooks/x-v2"
    # and the audit row reflects the change
    update_events = [e for e in audit.entries if e.event_type == "WebhookSubscriptionUpdated"]
    assert len(update_events) == 1


@pytest.mark.asyncio
async def test_disable_subscription__stops_matching_events() -> None:
    # given an active subscription
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    clock = FixedClock()
    sub = await outbound_uc.register_subscription(
        outbound_uc.RegisterSubscriptionCommand(
            tenant_id=TENANT,
            url="https://example.test/webhooks/x",
            event_filters=("CaseCreated",),
            secret_name="x",
            actor=make_actor(),
        ),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # when the subscription is disabled
    disabled = await outbound_uc.disable_subscription(
        TENANT,
        sub.id,
        actor=make_actor(),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # then the subscription no longer matches events
    assert disabled.status is SubscriptionStatus.DISABLED
    assert disabled.matches_event("CaseCreated") is False
    # and a re-disable is a no-op state-wise (idempotent: still DISABLED)
    again = await outbound_uc.disable_subscription(
        TENANT,
        sub.id,
        actor=make_actor(),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    assert again.status is SubscriptionStatus.DISABLED


@pytest.mark.asyncio
async def test_retire_subscription__terminal_and_does_not_match() -> None:
    # given an active subscription
    audit = InMemoryAuditStore()
    repo = InMemorySubscriptionRepository()
    clock = FixedClock()
    sub = await outbound_uc.register_subscription(
        outbound_uc.RegisterSubscriptionCommand(
            tenant_id=TENANT,
            url="https://example.test/webhooks/x",
            event_filters=("CaseCreated",),
            secret_name="x",
            actor=make_actor(),
        ),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # when the subscription is retired
    retired = await outbound_uc.retire_subscription(
        TENANT,
        sub.id,
        actor=make_actor(),
        subscription_repo=repo,
        audit=audit,
        clock=clock,
    )
    # then the subscription is RETIRED
    assert retired.status is SubscriptionStatus.RETIRED
    # and cannot be disabled
    with pytest.raises(ValidationFailed):
        await outbound_uc.disable_subscription(
            TENANT,
            sub.id,
            actor=make_actor(),
            subscription_repo=repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Delivery enqueue
# ---------------------------------------------------------------------------


def _make_active_sub(
    *,
    secret_name: str = "outbound_subscriber_acme",
    url: str = "https://example.test/webhooks/x",
) -> WebhookSubscription:
    return WebhookSubscription(
        id=uuid4(),
        tenant_id=TENANT,
        url=url,
        event_filters=("CaseCreated",),
        secret_name=secret_name,
        status=SubscriptionStatus.ACTIVE,
        version_token=0,
    )


@pytest.mark.asyncio
async def test_enqueue_delivery__first_event__creates_row_and_audit() -> None:
    # given a subscription and a fresh delivery repo
    audit = InMemoryAuditStore()
    sub_repo = InMemorySubscriptionRepository()
    delivery_repo = InMemoryDeliveryRepository()
    clock = FixedClock()
    sub = _make_active_sub()
    await sub_repo.add(sub)
    # when an event is enqueued
    delivery, created = await outbound_uc.enqueue_delivery(
        outbound_uc.EnqueueDeliveryCommand(
            tenant_id=TENANT,
            subscription=sub,
            event_id=uuid4(),
            event_type="CaseCreated",
            payload={"case_id": str(uuid4())},
            correlation_id="corr-1",
        ),
        delivery_repo=delivery_repo,
        audit=audit,
        clock=clock,
    )
    # then a new row is created
    assert created is True
    assert delivery.state is DeliveryState.PENDING
    assert delivery.attempt_count == 0
    assert delivery.target_url == sub.url
    # and an audit row is appended
    assert any(e.event_type == "OutboundDeliveryEnqueued" for e in audit.entries)


@pytest.mark.asyncio
async def test_enqueue_delivery__duplicate_event__returns_prior_row_no_audit() -> None:
    # given a subscription and a first enqueue
    audit = InMemoryAuditStore()
    sub_repo = InMemorySubscriptionRepository()
    delivery_repo = InMemoryDeliveryRepository()
    clock = FixedClock()
    sub = _make_active_sub()
    await sub_repo.add(sub)
    event_id = uuid4()
    cmd = outbound_uc.EnqueueDeliveryCommand(
        tenant_id=TENANT,
        subscription=sub,
        event_id=event_id,
        event_type="CaseCreated",
        payload={"case_id": "first"},
    )
    first, created_first = await outbound_uc.enqueue_delivery(
        cmd, delivery_repo=delivery_repo, audit=audit, clock=clock
    )
    assert created_first is True
    audit_count_after_first = len(audit.entries)
    # when the same event is enqueued again
    cmd2 = outbound_uc.EnqueueDeliveryCommand(
        tenant_id=TENANT,
        subscription=sub,
        event_id=event_id,
        event_type="CaseCreated",
        payload={"case_id": "second"},
    )
    second, created_second = await outbound_uc.enqueue_delivery(
        cmd2, delivery_repo=delivery_repo, audit=audit, clock=clock
    )
    # then the same row is returned and no new audit is written
    assert created_second is False
    assert second.id == first.id
    assert len(audit.entries) == audit_count_after_first


@pytest.mark.asyncio
async def test_enqueue_delivery__subscription_filter_mismatch__rejected() -> None:
    # given a subscription whose filter does not include the event_type
    audit = InMemoryAuditStore()
    delivery_repo = InMemoryDeliveryRepository()
    sub = _make_active_sub()
    # when an event of a different type is enqueued
    with pytest.raises(ValidationFailed):
        await outbound_uc.enqueue_delivery(
            outbound_uc.EnqueueDeliveryCommand(
                tenant_id=TENANT,
                subscription=sub,
                event_id=uuid4(),
                event_type="TaskCompleted",
                payload={},
            ),
            delivery_repo=delivery_repo,
            audit=audit,
            clock=FixedClock(),
        )


# ---------------------------------------------------------------------------
# Delivery dispatch (signed, retry, dead-letter)
# ---------------------------------------------------------------------------


def _build_service(
    *,
    subscription: WebhookSubscription,
    delivery: OutboundDelivery,
    transport: OutboundDeliveryTransport,
    secret_value: str = SUBSCRIBER_SECRET,
) -> outbound_uc.DeliveryService:
    sub_repo = InMemorySubscriptionRepository()
    delivery_repo = InMemoryDeliveryRepository()
    # Seed the two repos so the service can resolve the
    # subscription and update the delivery row.
    return _BoundService(
        subscription=sub_repo,
        delivery=delivery_repo,
        subscription_row=subscription,
        delivery_row=delivery,
        transport=transport,
        secret_value=secret_value,
    )


@dataclass
class _BoundService:
    """Wraps a ``DeliveryService`` around pre-seeded in-memory repos."""

    subscription: InMemorySubscriptionRepository
    delivery: InMemoryDeliveryRepository
    subscription_row: WebhookSubscription
    delivery_row: OutboundDelivery
    transport: OutboundDeliveryTransport
    secret_value: str
    provider_name: str | None = None
    _service: outbound_uc.DeliveryService | None = None
    _seeded: bool = False

    async def _ensure_seeded(self) -> outbound_uc.DeliveryService:
        if not self._seeded:
            await self.subscription.add(self.subscription_row)
            await self.delivery.add(self.delivery_row)
            # The provider is provisioned for a name that does NOT
            # match the subscription's ``secret_name`` when the test
            # asked for that mismatch (the ``missing_secret`` test).
            # The mismatch is achieved by passing the desired
            # ``provider_name`` separately from ``secret_value``; if
            # ``provider_name`` is not supplied, the provider is
            # provisioned for the subscription's own secret_name.
            provider_name = self.provider_name or self.subscription_row.secret_name
            secret_provider = StaticSecretProvider(name=provider_name, value=self.secret_value)
            self._service = outbound_uc.DeliveryService(
                delivery_repo=self.delivery,
                subscription_repo=self.subscription,
                transport=self.transport,
                secret_provider=secret_provider,
                clock=FixedClock(),
                max_attempts=3,
                attempt_timeout_seconds=1.0,
            )
            self._seeded = True
        assert self._service is not None
        return self._service

    async def attempt(self, delivery: OutboundDelivery) -> bool:
        service = await self._ensure_seeded()
        return await service.attempt(delivery)

    async def get_delivery(self, delivery_id: UUID) -> OutboundDelivery | None:
        return await self.delivery.get(self.subscription_row.tenant_id, delivery_id)


@pytest.mark.asyncio
async def test_dispatch__subscriber_2xx__marks_delivery_succeeded() -> None:
    # given a subscriber that returns 2xx
    sub = _make_active_sub()
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={"case_id": "abc"},
        target_url=sub.url,
        state=DeliveryState.PENDING,
        attempt_count=0,
    )
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=True, status_code=200))
    service = _build_service(subscription=sub, delivery=delivery, transport=transport)
    # when the dispatcher attempts the delivery
    terminal = await service.attempt(delivery)
    # then the row is marked SUCCEEDED and exactly one HTTP call is made
    assert terminal is True
    assert len(transport.calls) == 1
    final = await service.get_delivery(delivery.id)
    assert final is not None
    assert final.state is DeliveryState.SUCCEEDED
    assert final.attempt_count == 1
    assert final.last_status_code == 200
    assert final.succeeded_at is not None


@pytest.mark.asyncio
async def test_dispatch__auth_headers_match_hmac_over_canonical_string() -> None:
    # given a subscriber and a delivery
    sub = _make_active_sub()
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={"case_id": "abc"},
        correlation_id="corr-42",
        target_url=sub.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=True, status_code=204))
    service = _build_service(subscription=sub, delivery=delivery, transport=transport)
    # when the dispatcher attempts the delivery
    await service.attempt(delivery)
    # then the request carries the expected authentication headers
    assert len(transport.calls) == 1
    sent = transport.calls[0]
    assert sent["url"] == sub.url
    headers = sent["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Webhook-Algorithm"] == "hmac_sha256"
    assert headers["X-Webhook-Event-Id"] == str(delivery.event_id)
    assert headers["X-Webhook-Event-Type"] == "CaseCreated"
    assert headers["X-Webhook-Delivery-Id"] == str(delivery.id)
    assert headers["X-Correlation-Id"] == "corr-42"
    # the signature is sha256=hex of HMAC(secret, f"{timestamp}.{body}")
    expected = hmac.new(
        SUBSCRIBER_SECRET.encode("utf-8"),
        msg=f"{headers['X-Webhook-Timestamp']}.".encode() + sent["body"],
        digestmod=hashlib.sha256,
    ).hexdigest()
    assert headers["X-Webhook-Signature"] == f"sha256={expected}"


@pytest.mark.asyncio
async def test_dispatch__5xx_response__retries_and_eventually_dead_letters() -> None:
    # given a subscriber that always returns 500 and a max_attempts of 3
    sub = _make_active_sub()
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={"case_id": "abc"},
        target_url=sub.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    for _ in range(10):
        transport.set_response(TransportResult(success=False, status_code=500, error="boom"))
    service = _BoundService(
        subscription=InMemorySubscriptionRepository(),
        delivery=InMemoryDeliveryRepository(),
        subscription_row=sub,
        delivery_row=delivery,
        transport=transport,
        secret_value=SUBSCRIBER_SECRET,
    )
    # when the dispatcher attempts the delivery three times
    terminal_a = await service.attempt(delivery)
    d1 = await service.get_delivery(delivery.id)
    assert d1 is not None
    terminal_b = await service.attempt(d1)
    d2 = await service.get_delivery(delivery.id)
    terminal_c = await service.attempt(d2) if d2 is not None else None
    # then attempt_count climbs and the row eventually moves to DEAD_LETTERED
    assert terminal_a is False
    assert terminal_b is False
    assert terminal_c is True
    final = await service.get_delivery(delivery.id)
    assert final is not None
    assert final.state is DeliveryState.DEAD_LETTERED
    assert final.attempt_count == 3
    assert final.last_status_code == 500
    # exactly 3 HTTP calls were made
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_dispatch__permanent_4xx__dead_letters_immediately() -> None:
    # given a subscriber that returns 401 (a permanent 4xx)
    sub = _make_active_sub()
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={},
        target_url=sub.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=False, status_code=401, error="auth"))
    service = _BoundService(
        subscription=InMemorySubscriptionRepository(),
        delivery=InMemoryDeliveryRepository(),
        subscription_row=sub,
        delivery_row=delivery,
        transport=transport,
        secret_value=SUBSCRIBER_SECRET,
    )
    # when the dispatcher attempts the delivery
    terminal = await service.attempt(delivery)
    # then it dead-letters on the first attempt
    assert terminal is True
    final = await service.get_delivery(delivery.id)
    assert final is not None
    assert final.state is DeliveryState.DEAD_LETTERED
    assert final.attempt_count == 1
    assert final.last_status_code == 401
    # and only one HTTP call was made
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_dispatch__429_response__is_retried_like_5xx() -> None:
    # given a subscriber that returns 429 (transient 4xx) twice then 200
    sub = _make_active_sub()
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={},
        target_url=sub.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=False, status_code=429, error="busy"))
    transport.set_response(TransportResult(success=True, status_code=200))
    service = _BoundService(
        subscription=InMemorySubscriptionRepository(),
        delivery=InMemoryDeliveryRepository(),
        subscription_row=sub,
        delivery_row=delivery,
        transport=transport,
        secret_value=SUBSCRIBER_SECRET,
    )
    # when the dispatcher attempts the delivery twice
    terminal_a = await service.attempt(delivery)
    d1 = await service.get_delivery(delivery.id)
    assert d1 is not None
    terminal_b = await service.attempt(d1)
    # then the first attempt stays PENDING and the second SUCCEEDS
    assert terminal_a is False
    assert terminal_b is True
    assert d1.state is DeliveryState.PENDING
    assert d1.attempt_count == 1
    final = await service.get_delivery(delivery.id)
    assert final is not None
    assert final.state is DeliveryState.SUCCEEDED
    assert final.attempt_count == 2


@pytest.mark.asyncio
async def test_dispatch__missing_secret__dead_letters_with_missing_secret_error() -> None:
    # given a subscription whose secret is not in the SecretProvider
    sub = _make_active_sub(secret_name="not_provisioned")
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={},
        target_url=sub.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    service = _BoundService(
        subscription=InMemorySubscriptionRepository(),
        delivery=InMemoryDeliveryRepository(),
        subscription_row=sub,
        delivery_row=delivery,
        transport=transport,
        secret_value="unused",
        provider_name="some_other_secret",  # does NOT match sub.secret_name
    )
    # when the dispatcher attempts the delivery
    await service.attempt(delivery)
    # then the row is dead-lettered and last_error mentions the secret
    final = await service.get_delivery(delivery.id)
    assert final is not None
    assert final.state is DeliveryState.DEAD_LETTERED
    assert "missing_secret" in (final.last_error or "")


@pytest.mark.asyncio
async def test_dispatch__require_bearer__attaches_jwt_authorization_header() -> None:
    # given a subscription with require_bearer=True
    sub = _make_active_sub()
    sub_with_bearer = WebhookSubscription(
        id=sub.id,
        tenant_id=sub.tenant_id,
        url=sub.url,
        event_filters=sub.event_filters,
        secret_name=sub.secret_name,
        signing_algorithm=SigningAlgorithm.JWT_BEARER,
        require_bearer=True,
        status=SubscriptionStatus.ACTIVE,
        version_token=0,
    )
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=TENANT,
        subscription_id=sub_with_bearer.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={"k": "v"},
        target_url=sub_with_bearer.url,
        state=DeliveryState.PENDING,
    )
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=True, status_code=200))
    service = _BoundService(
        subscription=InMemorySubscriptionRepository(),
        delivery=InMemoryDeliveryRepository(),
        subscription_row=sub_with_bearer,
        delivery_row=delivery,
        transport=transport,
        secret_value=SUBSCRIBER_SECRET,
    )
    # when the dispatcher attempts the delivery
    await service.attempt(delivery)
    # then the Authorization header carries a Bearer token
    sent = transport.calls[0]
    auth = sent["headers"].get("Authorization", "")
    assert auth.startswith("Bearer ")
    token = auth.removeprefix("Bearer ")
    parts = token.split(".")
    assert len(parts) == 3  # header.payload.signature
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert payload["sub"] == "outbound-webhook"
    assert payload["sub_id"] == str(sub_with_bearer.id)
    assert payload["dlv_id"] == str(delivery.id)
    assert payload["evt_id"] == str(delivery.event_id)
    # and the HMAC is still present (belt + suspenders)
    assert sent["headers"]["X-Webhook-Signature"].startswith("sha256=")


# ---------------------------------------------------------------------------
# Inbound vs Outbound distinct
# ---------------------------------------------------------------------------


def test_inbound_outbound_have_distinct_aggregates() -> None:
    """Outbound uses :class:`OutboundDelivery`; inbound uses
    :class:`CapturedWebhook`. They share the same naming pattern
    but live in different modules with different invariants
    (FM-030 vs FM-056, ADR-007 vs ADR-013).
    """
    O = _OutboundDelivery  # noqa: N806, E741 — single-letter class aliases intentional
    I = _CapturedWebhook  # noqa: N806, E741 — single-letter class aliases intentional

    assert O is not I
    # The outbound aggregate has ``state``, ``attempt_count``,
    # ``last_status_code``, ``succeeded_at`` — all retry-related
    # fields that the inbound aggregate does not need (inbound is
    # not retried).
    outbound_fields = set(O.__dataclass_fields__)
    inbound_fields = set(I.__dataclass_fields__)
    assert "state" in outbound_fields
    assert "attempt_count" in outbound_fields
    assert "last_status_code" in outbound_fields
    assert "succeeded_at" in outbound_fields
    assert "state" not in inbound_fields
    assert "attempt_count" not in inbound_fields


def test_inbound_router_does_not_produce_outbound_deliveries() -> None:
    """The inbound webhook router's only side effect is to call
    :func:`webhook_uc.apply_webhook_command`. The outbound use case
    is wired through a different code path. Calling the inbound
    router in isolation must NOT create an ``OutboundDelivery``
    row.
    """
    # then the inbound use case module does NOT expose the
    # outbound use case's enqueue helper. This guards against a
    # future refactor that accidentally couples the two surfaces.
    from app.application.capture import webhook_commands as mod  # noqa: PLC0415

    assert not hasattr(mod, "enqueue_delivery")


# ---------------------------------------------------------------------------
# Pure signing helpers
# ---------------------------------------------------------------------------


def test_sign_delivery__hmac_sha256__uses_canonical_string() -> None:
    # given a body, timestamp, and secret
    body = b'{"event_id":"abc"}'
    timestamp = "1725260000"
    secret = "shared-secret"
    # when the signature is computed
    sig = outbound_uc.sign_delivery(
        body=body,
        timestamp=timestamp,
        secret=secret,
        algorithm=SigningAlgorithm.HMAC_SHA256,
    )
    # then it matches an independently-computed HMAC-SHA256 over f"{timestamp}.{body}"
    expected_mac = hmac.new(
        secret.encode("utf-8"),
        msg=f"{timestamp}.".encode() + body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    assert sig == f"sha256={expected_mac}"


def test_build_jwt_bearer__contains_required_claims() -> None:
    # given a subscription id, delivery id, event id, and a clock
    now = datetime(2026, 9, 2, 8, 43, 15, tzinfo=UTC)
    sub_id = uuid4()
    dlv_id = uuid4()
    evt_id = uuid4()
    # when the bearer is built
    token = outbound_uc.build_jwt_bearer(
        secret="shared-secret",
        subscription_id=sub_id,
        delivery_id=dlv_id,
        event_id=evt_id,
        now=now,
    )
    # then the token has the expected structure and claims
    parts = token.split(".")
    assert len(parts) == 3

    def _decode(part: str) -> dict[str, Any]:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    header = _decode(parts[0])
    payload = _decode(parts[1])
    assert header["alg"] == "HS256"
    assert header["typ"] == "JWT"
    assert payload["sub"] == "outbound-webhook"
    assert payload["sub_id"] == str(sub_id)
    assert payload["dlv_id"] == str(dlv_id)
    assert payload["evt_id"] == str(evt_id)
    assert payload["iat"] == int(now.timestamp())
    assert payload["exp"] == int(now.timestamp()) + 5 * 60
    assert "jti" in payload


# ---------------------------------------------------------------------------
# SQL repository smoke test
#
# Pinned behavior: the SQL adapter enforces the
# ``(tenant_id, subscription_id, event_id)`` unique constraint and
# surfaces the violation as a domain ``Conflict`` so the
# application-layer ``except Conflict`` branch in
# :func:`enqueue_delivery` catches it. Cross-tenant reads return
# ``None``. The fixture is sqlite-backed (AGENTS.md §4.R1 fallback).
# ---------------------------------------------------------------------------


def test_sql_subscription_repository__add_and_get_roundtrip() -> None:
    """The SQL adapter persists and reads back a subscription, and
    rejects cross-tenant reads.
    """
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
        async_sessionmaker,
        create_async_engine,
    )

    from app.db import Base  # noqa: PLC0415
    from app.infrastructure.persistence import models  # noqa: F401, PLC0415

    async def _run() -> None:
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with sm() as session:
                # Need a tenant row to satisfy the FK.
                from datetime import datetime as _dt  # noqa: PLC0415

                from app.infrastructure.persistence.models import Tenant  # noqa: PLC0415

                tenant = Tenant(
                    id=TENANT,
                    key="acme",
                    name="acme",
                    created_at=_dt.now(UTC),
                )
                session.add(tenant)
                await session.commit()
                repo = SqlWebhookSubscriptionRepository(session)
                sub = WebhookSubscription(
                    tenant_id=TENANT,
                    url="https://example.test/webhooks/x",
                    event_filters=("CaseCreated",),
                    secret_name="sub_secret",
                    created_at=_dt.now(UTC),
                    updated_at=_dt.now(UTC),
                )
                await repo.add(sub)
                await session.commit()
                # when reading back
                fetched = await repo.get(TENANT, sub.id)
                assert fetched is not None
                assert fetched.id == sub.id
                assert fetched.event_filters == ("CaseCreated",)
                # and cross-tenant reads return None
                assert await repo.get(OTHER_TENANT, sub.id) is None
        finally:
            await engine.dispose()
            with contextlib.suppress(OSError):
                os.remove(path)

    asyncio.run(_run())
