"""Webhook delivery dispatcher tests (FM-056 / P13-US10).

Pinned behaviors:

1. **Drain delivers** — ``drain_once`` builds one ``DeliveryService``
   per tenant and calls ``dispatch_pending``; a 2xx POST marks the row
   ``SUCCEEDED`` and counts it as terminal.
2. **Retry then dead-letter** — repeated 5xx responses bump the attempt
   counter (row stays ``PENDING``, not counted) until the cap, at which
   point the row moves to ``DEAD_LETTERED`` and counts as terminal.
3. **Disabled subscriptions are skipped** — a pending row whose
   subscription is ``DISABLED`` (or retired) causes no HTTP POST, keeps
   its attempt counter, stays ``PENDING``, and is not counted.
4. **Empty tenants drain zero** — no tenants means no dispatch and no
   service construction.
5. **Tick never dies** — a transient enumeration failure is logged and
   the next tick still drains.
6. **SQL-backed drain commits** — the default (non-fake) path persists
   the terminal state through ``sql_session_factory``.

Tests follow AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given / // when / // then`` per test. The first five tests use
in-memory fakes (no database); the last one uses the shared
``sql_session_factory`` fixture (sqlite, tenant listener disabled).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.webhooks import outbound as outbound_uc
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    SubscriptionStatus,
    WebhookSubscription,
)
from app.domain.ports import (
    OutboundDeliveryRepository,
    SecretProvider,
    TransportResult,
    WebhookSubscriptionRepository,
)
from app.infrastructure.persistence.models import Tenant
from app.infrastructure.persistence.repositories import (
    SqlOutboundDeliveryRepository,
    SqlWebhookSubscriptionRepository,
)
from app.infrastructure.webhooks.delivery_dispatcher import WebhookDeliveryDispatcher
from app.infrastructure.webhooks.httpx_transport import RecordingOutboundTransport

TENANT = UUID("11111111-1111-1111-1111-111111111111")
FIXED_NOW = datetime(2026, 9, 2, 8, 43, 15, tzinfo=UTC)
SECRET = "outbound_subscriber_acme_secret_value_12345"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/webhooks/test_outbound.py harness)
# ---------------------------------------------------------------------------


class _FixedClock:
    """Deterministic clock pinned to ``FIXED_NOW`` (advanceable)."""

    def __init__(self) -> None:
        self._now = FIXED_NOW

    def now(self) -> datetime:
        """Return the fixed instant."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the instant forward (mirrors the outbox test harness)."""
        self._now = self._now + timedelta(seconds=seconds)


class _InMemorySubscriptionRepository(WebhookSubscriptionRepository):
    """Tenant-scoped in-memory subscription store."""

    def __init__(self) -> None:
        self._store: dict[UUID, WebhookSubscription] = {}

    async def get(self, tenant_id: UUID, subscription_id: UUID) -> WebhookSubscription | None:
        """Return the row, or ``None`` for missing / cross-tenant ids."""
        sub = self._store.get(subscription_id)
        if sub is None or sub.tenant_id != tenant_id:
            return None
        return sub

    async def list(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[WebhookSubscription]:
        """List this tenant's subscriptions (insertion order)."""
        rows = [s for s in self._store.values() if s.tenant_id == tenant_id]
        return rows[offset : offset + limit]

    async def add(self, subscription: WebhookSubscription) -> None:
        """Insert a subscription row."""
        self._store[subscription.id] = subscription

    async def update(self, subscription: WebhookSubscription) -> None:
        """Upsert a subscription row."""
        self._store[subscription.id] = subscription


class _InMemoryDeliveryRepository(OutboundDeliveryRepository):
    """Tenant-scoped in-memory delivery store."""

    def __init__(self) -> None:
        self._store: dict[UUID, OutboundDelivery] = {}

    async def get(self, tenant_id: UUID, delivery_id: UUID) -> OutboundDelivery | None:
        """Return the row, or ``None`` for missing / cross-tenant ids."""
        row = self._store.get(delivery_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_event(
        self, tenant_id: UUID, subscription_id: UUID, event_id: UUID
    ) -> OutboundDelivery | None:
        """Return the prior row for this (subscription, event) pair."""
        for row in self._store.values():
            if (
                row.tenant_id == tenant_id
                and row.subscription_id == subscription_id
                and row.event_id == event_id
            ):
                return row
        return None

    async def list_pending(
        self,
        tenant_id: UUID,
        *,
        subscription_id: UUID | None = None,
        limit: int = 50,
    ) -> list[OutboundDelivery]:
        """Return pending rows, oldest first."""
        rows = [
            d
            for d in self._store.values()
            if d.tenant_id == tenant_id and d.state is DeliveryState.PENDING
        ]
        rows.sort(key=lambda d: d.created_at or datetime.min.replace(tzinfo=UTC))
        if subscription_id is not None:
            rows = [d for d in rows if d.subscription_id == subscription_id]
        return rows[:limit]

    async def add(self, delivery: OutboundDelivery) -> None:
        """Insert a delivery row."""
        self._store[delivery.id] = delivery

    async def update(self, delivery: OutboundDelivery) -> None:
        """Replace a delivery row."""
        self._store[delivery.id] = delivery

    async def list_dead_lettered(
        self, tenant_id: UUID, *, limit: int = 50
    ) -> list[OutboundDelivery]:
        """Return dead-lettered rows for the tenant."""
        return [
            d
            for d in self._store.values()
            if d.tenant_id == tenant_id and d.state is DeliveryState.DEAD_LETTERED
        ][:limit]


class _StaticSecretProvider(SecretProvider):
    """Return a fixed value for one logical secret name."""

    def __init__(self, *, name: str, value: str) -> None:
        self._name = name
        self._value = value

    async def get(self, name: str) -> str:
        """Return the value, or raise ``KeyError`` on name mismatch."""
        if name != self._name:
            raise KeyError(name)
        return self._value

    async def set_secret(self, name: str, value: str) -> None:
        """No-op (local-dev convenience only)."""


def _make_sub(
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    tenant_id: UUID = TENANT,
) -> WebhookSubscription:
    """Build an ACTIVE (by default) subscription entity for the tenant."""
    return WebhookSubscription(
        id=uuid4(),
        tenant_id=tenant_id,
        url="https://example.test/webhooks/x",
        event_filters=("CaseCreated",),
        secret_name="outbound_subscriber_acme",
        status=status,
        version_token=0,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _make_delivery(sub: WebhookSubscription) -> OutboundDelivery:
    """Build a PENDING delivery entity targeting ``sub``."""
    return OutboundDelivery(
        id=uuid4(),
        tenant_id=sub.tenant_id,
        subscription_id=sub.id,
        event_id=uuid4(),
        event_type="CaseCreated",
        payload_json={"case_id": "abc"},
        target_url=sub.url,
        state=DeliveryState.PENDING,
        attempt_count=0,
        created_at=FIXED_NOW,
    )


def _dispatcher_for(
    sub_repo: _InMemorySubscriptionRepository,
    delivery_repo: _InMemoryDeliveryRepository,
    transport: RecordingOutboundTransport,
    *,
    sub: WebhookSubscription,
    max_attempts: int = 3,
    tenants: list[UUID] | None = None,
    clock: _FixedClock | None = None,
) -> WebhookDeliveryDispatcher:
    """Build a dispatcher whose service factory owns the in-memory fakes.

    No database is touched: tenant enumeration and service construction
    are both injected, so ``sessionmaker`` stays ``None``.
    """
    shared_clock = clock if clock is not None else _FixedClock()

    def _factory(tenant_id: UUID) -> outbound_uc.DeliveryService:
        return outbound_uc.DeliveryService(
            delivery_repo=delivery_repo,
            subscription_repo=sub_repo,
            transport=transport,
            secret_provider=_StaticSecretProvider(name=sub.secret_name, value=SECRET),
            clock=shared_clock,
            max_attempts=max_attempts,
        )

    async def _tenants() -> list[UUID]:
        return list(tenants) if tenants is not None else [TENANT]

    return WebhookDeliveryDispatcher(
        service_factory=_factory,
        tenant_ids_provider=_tenants,
        sleep_seconds=0.01,
    )


# ---------------------------------------------------------------------------
# Drain behavior (in-memory fakes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_pending_row__dispatches_and_marks_succeeded() -> None:
    # given a pending delivery for an ACTIVE subscription and a 2xx stub
    sub_repo = _InMemorySubscriptionRepository()
    delivery_repo = _InMemoryDeliveryRepository()
    sub = _make_sub()
    delivery = _make_delivery(sub)
    await sub_repo.add(sub)
    await delivery_repo.add(delivery)
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=True, status_code=200))
    dispatcher = _dispatcher_for(sub_repo, delivery_repo, transport, sub=sub)
    # when the dispatcher drains
    drained = await dispatcher.drain_once(batch_size=10)
    # then one terminal delivery, one POST, and the row is SUCCEEDED
    assert drained == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"] == sub.url
    row = await delivery_repo.get(TENANT, delivery.id)
    assert row is not None
    assert row.state is DeliveryState.SUCCEEDED
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_drain_failed_posts__retries_then_dead_letters_after_cap() -> None:
    # given a pending delivery and a stub that always returns 500
    sub_repo = _InMemorySubscriptionRepository()
    delivery_repo = _InMemoryDeliveryRepository()
    sub = _make_sub()
    delivery = _make_delivery(sub)
    await sub_repo.add(sub)
    await delivery_repo.add(delivery)
    transport = RecordingOutboundTransport()
    for _ in range(3):
        transport.set_response(TransportResult(success=False, status_code=500, error="http 500"))
    clock = _FixedClock()
    dispatcher = _dispatcher_for(
        sub_repo, delivery_repo, transport, sub=sub, max_attempts=3, clock=clock
    )
    # when the dispatcher drains ...
    first = await dispatcher.drain_once(batch_size=10)
    # ... the first drain retries without counting terminal ...
    assert first == 0
    row = await delivery_repo.get(TENANT, delivery.id)
    assert row is not None
    assert row.state is DeliveryState.PENDING
    assert row.attempt_count == 1
    # ... and an immediate second drain is skipped inside the backoff
    # window (no POST, counter untouched) ...
    immediate = await dispatcher.drain_once(batch_size=10)
    assert immediate == 0
    assert len(transport.calls) == 1
    # ... and drains past the backoff windows (5s, then 10s) retry ...
    clock.advance(6)
    second = await dispatcher.drain_once(batch_size=10)
    clock.advance(11)
    third = await dispatcher.drain_once(batch_size=10)
    assert second == 0
    # ... the cap dead-letters the row and counts terminal
    assert third == 1
    row = await delivery_repo.get(TENANT, delivery.id)
    assert row is not None
    assert row.state is DeliveryState.DEAD_LETTERED
    assert row.attempt_count == 3
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_drain_disabled_subscription__skips_without_posting() -> None:
    # given a pending delivery whose subscription has been DISABLED
    sub_repo = _InMemorySubscriptionRepository()
    delivery_repo = _InMemoryDeliveryRepository()
    sub = _make_sub(status=SubscriptionStatus.DISABLED)
    delivery = _make_delivery(sub)
    await sub_repo.add(sub)
    await delivery_repo.add(delivery)
    transport = RecordingOutboundTransport()
    dispatcher = _dispatcher_for(sub_repo, delivery_repo, transport, sub=sub)
    # when the dispatcher drains
    drained = await dispatcher.drain_once(batch_size=10)
    # then nothing is counted, nothing is POSTed, and the row is untouched
    assert drained == 0
    assert transport.calls == []
    row = await delivery_repo.get(TENANT, delivery.id)
    assert row is not None
    assert row.state is DeliveryState.PENDING
    assert row.attempt_count == 0


@pytest.mark.asyncio
async def test_drain_no_tenants__returns_zero_without_dispatch() -> None:
    # given a dispatcher with no tenants and a factory that must not run
    invoked: list[UUID] = []

    def _factory(tenant_id: UUID) -> outbound_uc.DeliveryService:
        invoked.append(tenant_id)
        raise AssertionError("service factory must not run with zero tenants")

    async def _no_tenants() -> list[UUID]:
        return []

    dispatcher = WebhookDeliveryDispatcher(
        service_factory=_factory,
        tenant_ids_provider=_no_tenants,
        sleep_seconds=0.01,
    )
    # when the dispatcher drains
    drained = await dispatcher.drain_once(batch_size=10)
    # then zero is returned and no service is constructed
    assert drained == 0
    assert invoked == []


@pytest.mark.asyncio
async def test_tick_failing_first_drain__loop_survives_and_drains_next_tick() -> None:
    # given tenant enumeration that fails once, then recovers
    sub_repo = _InMemorySubscriptionRepository()
    delivery_repo = _InMemoryDeliveryRepository()
    sub = _make_sub()
    delivery = _make_delivery(sub)
    await sub_repo.add(sub)
    await delivery_repo.add(delivery)
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=True, status_code=200))
    attempts = {"n": 0}

    async def _flaky_tenants() -> list[UUID]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient tenant-list failure")
        return [TENANT]

    dispatcher = WebhookDeliveryDispatcher(
        service_factory=lambda tenant_id: outbound_uc.DeliveryService(
            delivery_repo=delivery_repo,
            subscription_repo=sub_repo,
            transport=transport,
            secret_provider=_StaticSecretProvider(name=sub.secret_name, value=SECRET),
            clock=_FixedClock(),
            max_attempts=3,
        ),
        tenant_ids_provider=_flaky_tenants,
        sleep_seconds=0.01,
    )
    # when the tick loop runs past the transient failure
    task = asyncio.create_task(dispatcher.tick())
    try:
        for _ in range(200):
            row = await delivery_repo.get(TENANT, delivery.id)
            if row is not None and row.state is DeliveryState.SUCCEEDED:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("tick did not drain after the transient failure")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    # then the loop survived (it retried) and the delivery succeeded
    assert attempts["n"] >= 2
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# SQL-backed drain (sqlite via sql_session_factory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_sql_backed__persists_succeeded_row(sql_session_factory: Any) -> None:
    # given a tenant + subscription + pending delivery in sqlite
    async with sql_session_factory() as session:
        session.add(Tenant(id=TENANT, key="acme", name="ACME", created_at=FIXED_NOW))
        await session.commit()
    sub = _make_sub()
    delivery = _make_delivery(sub)
    async with sql_session_factory() as session:
        await SqlWebhookSubscriptionRepository(session).add(sub)
        await SqlOutboundDeliveryRepository(session).add(delivery)
        await session.commit()
    transport = RecordingOutboundTransport()
    dispatcher = WebhookDeliveryDispatcher(
        sessionmaker=sql_session_factory,
        transport=transport,
        secret_provider=_StaticSecretProvider(name=sub.secret_name, value=SECRET),
        clock=_FixedClock(),
        sleep_seconds=0.01,
    )
    # when the dispatcher drains through the default SQL-backed path
    drained = await dispatcher.drain_once(batch_size=10)
    # then one terminal delivery, one POST, and the row is SUCCEEDED on disk
    assert drained == 1
    assert len(transport.calls) == 1
    async with sql_session_factory() as session:
        row = await SqlOutboundDeliveryRepository(session).get(TENANT, delivery.id)
    assert row is not None
    assert row.state is DeliveryState.SUCCEEDED
    assert row.attempt_count == 1
