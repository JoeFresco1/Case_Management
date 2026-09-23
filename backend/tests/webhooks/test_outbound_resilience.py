"""Outbound webhook resilience tests (FM-056 / P13-US10).

Pinned behaviors (acceptance for the delivery hardening slice):

1. **Timeout is a retriable failure** — a transport that raises
   (``TimeoutError``/``ConnectionError``) or returns
   ``status_code=None`` records ``attempt_count + 1`` and stays
   ``PENDING``; the row is retried after the backoff window and
   dead-letters at the cap. The drain never dies on a transport fault.
2. **Backoff gate** — ``dispatch_pending`` makes no POST and touches
   no counter while a failed row is inside its backoff window
   (5s/10s/20s/40s, the G-2 outbox schedule). A direct ``attempt``
   (manual replay) bypasses the gate.
3. **Poison dead-letters once** — a permanent 4xx dead-letters on the
   first attempt and the row is never POSTed again.
4. **Retirement stops delivery** — a ``RETIRED`` subscription (like a
   ``DISABLED`` one) causes no POST, no counter bump, and no terminal
   count; a retired subscription can never be disabled again.
5. **Safe duplicates** — the initial POST and its retry carry the same
   ``X-Webhook-Event-Id`` and byte-identical bodies, so a subscriber
   dedupes on the event id.
6. **No secret material in logs** — the subscriber secret value never
   appears in log records, error strings, audit payloads, stored rows,
   or wire headers (signatures are HMAC digests, not the secret).
7. **Fail-closed subscription auth** — registration without an actor,
   registration with an empty filter set, and disabling a retired
   subscription are all rejected.

Tests follow AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given / // when / // then``. All doubles are in-memory; the
suite never needs PostgreSQL and never makes a real HTTP request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.webhooks import outbound as outbound_uc
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.outbound_webhook import (
    DeliveryState,
    OutboundDelivery,
    SubscriptionStatus,
    WebhookSubscription,
    backoff_delay_seconds,
    delivery_due,
)
from app.domain.ports import (
    AuditEventStore,
    OutboundDeliveryRepository,
    SecretProvider,
    TransportResult,
    WebhookSubscriptionRepository,
)
from app.infrastructure.webhooks.httpx_transport import RecordingOutboundTransport

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SECRET = "outbound_resilience_probe_secret_value_9f8e7d6c"
FIXED_NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic, advanceable clock."""

    def __init__(self) -> None:
        self._now = FIXED_NOW

    def now(self) -> datetime:
        """Return the current instant."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the instant forward."""
        self._now = self._now + timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class _Actor:
    user_id: UUID = ACTOR
    tenant_id: UUID = TENANT
    display_name: str = "operator"
    roles: tuple[str, ...] = ("operator",)
    is_system: bool = False


class _Audit(AuditEventStore):
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        """Record the audit payload for secret-leak assertions."""
        self.entries.append(dict(kwargs))

    async def list_for_case(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Unused in this suite."""
        return []


class _Subs(WebhookSubscriptionRepository):
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
        """List this tenant's subscriptions."""
        return [s for s in self._store.values() if s.tenant_id == tenant_id][
            offset : offset + limit
        ]

    async def add(self, subscription: WebhookSubscription) -> None:
        """Insert a subscription row."""
        self._store[subscription.id] = subscription

    async def update(self, subscription: WebhookSubscription) -> None:
        """Replace a subscription row."""
        self._store[subscription.id] = subscription


class _Deliveries(OutboundDeliveryRepository):
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


class _Secrets(SecretProvider):
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


def _sub(
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    secret_name: str = "resilience_probe",
) -> WebhookSubscription:
    """Build a subscription entity for the tenant."""
    return WebhookSubscription(
        id=uuid4(),
        tenant_id=TENANT,
        url="https://example.test/webhooks/resilience",
        event_filters=("CaseCreated",),
        secret_name=secret_name,
        status=status,
        version_token=0,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        created_by=ACTOR,
    )


def _delivery(sub: WebhookSubscription) -> OutboundDelivery:
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


def _service(
    sub_repo: _Subs,
    delivery_repo: _Deliveries,
    transport: RecordingOutboundTransport,
    clock: _Clock,
    *,
    sub: WebhookSubscription,
    max_attempts: int = 3,
) -> outbound_uc.DeliveryService:
    """Build a DeliveryService over the in-memory fakes."""
    return outbound_uc.DeliveryService(
        delivery_repo=delivery_repo,
        subscription_repo=sub_repo,
        transport=transport,
        secret_provider=_Secrets(name=sub.secret_name, value=SECRET),
        clock=clock,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# Backoff schedule (pure domain helpers)
# ---------------------------------------------------------------------------


def test_backoff_delay_seconds__doubles_with_cap() -> None:
    # given the shared G-2 retry schedule
    # when the delay is computed, then it doubles per attempt and caps
    assert backoff_delay_seconds(1) == 5.0
    assert backoff_delay_seconds(2) == 10.0
    assert backoff_delay_seconds(3) == 20.0
    assert backoff_delay_seconds(4) == 40.0
    assert backoff_delay_seconds(10) == 300.0
    assert backoff_delay_seconds(0) == 5.0


def test_delivery_due__fresh_row_is_always_due() -> None:
    # given a delivery with no attempts yet
    row = _delivery(_sub())
    # when due is evaluated, then it is due even at the same instant
    assert delivery_due(row, FIXED_NOW) is True


def test_delivery_due__failed_row_respects_window() -> None:
    # given a delivery that failed once at FIXED_NOW
    row = _delivery(_sub()).record_attempt(
        status_code=500, error="boom", succeeded=False, now=FIXED_NOW, max_attempts=5
    )
    # when due is evaluated inside the 5s window, then it is not due ...
    assert delivery_due(row, FIXED_NOW + timedelta(seconds=4)) is False
    # ... and when the window elapses, then it is due
    assert delivery_due(row, FIXED_NOW + timedelta(seconds=5)) is True


def test_delivery_due__naive_datetimes_do_not_raise() -> None:
    # given datetimes as SQLite returns them (naive, UTC by convention)
    # why noqa DTZ001: the naive datetime IS the test — it reproduces
    # the SQLite read path that ``_ensure_aware`` must tolerate.
    naive = datetime(2026, 9, 22, 12, 0, 0)  # noqa: DTZ001
    row = _delivery(_sub()).record_attempt(
        status_code=500, error="boom", succeeded=False, now=naive, max_attempts=5
    )
    # when due is evaluated against an aware clock, then no TypeError ...
    assert delivery_due(row, naive + timedelta(seconds=4)) is False
    assert delivery_due(row, naive + timedelta(seconds=5)) is True


# ---------------------------------------------------------------------------
# Timeout (raising transport, and status_code=None results)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempt__transport_raises_timeout__recorded_as_retriable_failure() -> None:
    # given a transport that raises TimeoutError instead of returning
    subs, deliveries = _Subs(), _Deliveries()
    sub, row = _sub(), None
    row = _delivery(sub)
    await subs.add(sub)
    await deliveries.add(row)
    clock = _Clock()
    transport = RecordingOutboundTransport()
    transport.set_exception(TimeoutError("subscriber hung"))
    service = _service(subs, deliveries, transport, clock, sub=sub)
    # when the delivery is attempted
    terminal = await service.attempt(row)
    # then the row stays PENDING with one attempt, and the drain survives
    assert terminal is False
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert stored.state is DeliveryState.PENDING
    assert stored.attempt_count == 1
    assert stored.last_status_code is None
    assert stored.last_error is not None
    assert "TimeoutError" in stored.last_error
    assert SECRET not in stored.last_error


@pytest.mark.asyncio
async def test_attempt__timeout_result__retried_after_backoff_then_succeeds() -> None:
    # given a timeout result followed by a 2xx
    subs, deliveries = _Subs(), _Deliveries()
    sub = _sub()
    row = _delivery(sub)
    await subs.add(sub)
    await deliveries.add(row)
    clock = _Clock()
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=False, status_code=None, error="timeout"))
    transport.set_response(TransportResult(success=True, status_code=200))
    service = _service(subs, deliveries, transport, clock, sub=sub)
    # when the first attempt times out and the batch re-drains ...
    assert await service.attempt(row) is False
    immediate = await service.dispatch_pending(tenant_id=TENANT, batch_size=10)
    # ... then the immediate re-drain is skipped inside the backoff window ...
    assert immediate == 0
    assert len(transport.calls) == 1
    # ... and past the window the retry succeeds with the same event id
    clock.advance(6)
    assert await service.dispatch_pending(tenant_id=TENANT, batch_size=10) == 1
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert stored.state is DeliveryState.SUCCEEDED
    assert stored.attempt_count == 2
    assert len(transport.calls) == 2
    assert (
        transport.calls[0]["headers"]["X-Webhook-Event-Id"]
        == transport.calls[1]["headers"]["X-Webhook-Event-Id"]
        == str(row.event_id)
    )
    assert transport.calls[0]["body"] == transport.calls[1]["body"]


@pytest.mark.asyncio
async def test_attempt__transport_always_raises__dead_letters_at_cap() -> None:
    # given a transport that always raises and max_attempts=3
    subs, deliveries = _Subs(), _Deliveries()
    sub = _sub()
    row = _delivery(sub)
    await subs.add(sub)
    await deliveries.add(row)
    clock = _Clock()
    transport = RecordingOutboundTransport()
    for _ in range(3):
        transport.set_exception(ConnectionError("reset by peer"))
    service = _service(subs, deliveries, transport, clock, sub=sub, max_attempts=3)
    # when direct attempts (replay path, bypasses the backoff gate) run ...
    assert await service.attempt(row) is False
    d1 = await deliveries.get(TENANT, row.id)
    assert d1 is not None
    assert await service.attempt(d1) is False
    d2 = await deliveries.get(TENANT, row.id)
    assert d2 is not None
    assert await service.attempt(d2) is True
    # ... then the row dead-letters at the cap with a triageable error
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert stored.state is DeliveryState.DEAD_LETTERED
    assert stored.attempt_count == 3
    assert stored.last_error is not None
    assert "ConnectionError" in stored.last_error


# ---------------------------------------------------------------------------
# Poison (permanent 4xx dead-letters once and is never re-POSTed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain__poison_4xx__dead_letters_once_and_never_reposts() -> None:
    # given a subscriber that returns 422 (poison)
    subs, deliveries = _Subs(), _Deliveries()
    sub = _sub()
    row = _delivery(sub)
    await subs.add(sub)
    await deliveries.add(row)
    clock = _Clock()
    transport = RecordingOutboundTransport()
    transport.set_response(TransportResult(success=False, status_code=422, error="unprocessable"))
    service = _service(subs, deliveries, transport, clock, sub=sub)
    # when the batch drains twice
    assert await service.dispatch_pending(tenant_id=TENANT, batch_size=10) == 1
    assert await service.dispatch_pending(tenant_id=TENANT, batch_size=10) == 0
    # then exactly one POST was made and the row is dead-lettered
    assert len(transport.calls) == 1
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert stored.state is DeliveryState.DEAD_LETTERED
    assert stored.attempt_count == 1
    assert stored.last_status_code == 422


# ---------------------------------------------------------------------------
# Retirement (and disable-then-retire lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempt__retired_subscription__skips_without_posting() -> None:
    # given a pending delivery whose subscription has been RETIRED
    subs, deliveries = _Subs(), _Deliveries()
    sub = _sub(status=SubscriptionStatus.RETIRED)
    row = _delivery(sub)
    await subs.add(sub)
    await deliveries.add(row)
    clock = _Clock()
    transport = RecordingOutboundTransport()
    service = _service(subs, deliveries, transport, clock, sub=sub)
    # when the delivery is attempted (directly and via the batch)
    assert await service.attempt(row) is False
    assert await service.dispatch_pending(tenant_id=TENANT, batch_size=10) == 0
    # then nothing is POSTed and the row is untouched
    assert transport.calls == []
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert stored.state is DeliveryState.PENDING
    assert stored.attempt_count == 0


@pytest.mark.asyncio
async def test_disable_subscription__retired__rejected() -> None:
    # given a retired subscription row
    subs = _Subs()
    audit = _Audit()
    sub = _sub(status=SubscriptionStatus.RETIRED)
    await subs.add(sub)
    # when disabling is attempted, then it is rejected (terminal state)
    with pytest.raises(ValidationFailed):
        await outbound_uc.disable_subscription(
            TENANT,
            sub.id,
            actor=_Actor(),
            subscription_repo=subs,
            audit=audit,
            clock=_Clock(),
        )


# ---------------------------------------------------------------------------
# Fail-closed subscription auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_subscription__missing_actor__forbidden() -> None:
    # given a registration command with no actor
    subs = _Subs()
    audit = _Audit()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/x",
        event_filters=("CaseCreated",),
        secret_name="probe",
        actor=None,
    )
    # when registration runs, then it is refused and nothing is stored
    with pytest.raises(Forbidden):
        await outbound_uc.register_subscription(
            cmd, subscription_repo=subs, audit=audit, clock=_Clock()
        )
    assert await subs.list(TENANT) == []


@pytest.mark.asyncio
async def test_register_subscription__empty_filters__rejected() -> None:
    # given a registration command with an empty (match-everything?) filter
    subs = _Subs()
    audit = _Audit()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/x",
        event_filters=(),
        secret_name="probe",
        actor=_Actor(),
    )
    # when registration runs, then fail-closed validation fires
    with pytest.raises(ValidationFailed):
        await outbound_uc.register_subscription(
            cmd, subscription_repo=subs, audit=audit, clock=_Clock()
        )
    assert await subs.list(TENANT) == []


# ---------------------------------------------------------------------------
# No secret material in logs, rows, audit, or wire headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secret_material__in_logs_rows_audit_or_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # given a subscription + delivery that will fail then dead-letter
    subs, deliveries = _Subs(), _Deliveries()
    audit = _Audit()
    clock = _Clock()
    cmd = outbound_uc.RegisterSubscriptionCommand(
        tenant_id=TENANT,
        url="https://example.test/webhooks/x",
        event_filters=("CaseCreated",),
        secret_name="resilience_probe",
        actor=_Actor(),
    )
    sub = await outbound_uc.register_subscription(
        cmd, subscription_repo=subs, audit=audit, clock=clock
    )
    row = _delivery(sub)
    await deliveries.add(row)
    transport = RecordingOutboundTransport()
    for _ in range(3):
        transport.set_response(TransportResult(success=False, status_code=500, error="boom"))
    service = _service(subs, deliveries, transport, clock, sub=sub, max_attempts=3)
    # when the full lifecycle runs under captured logs
    with caplog.at_level(logging.DEBUG):
        assert await service.attempt(row) is False
        clock.advance(6)
        d1 = await deliveries.get(TENANT, row.id)
        assert d1 is not None
        assert await service.attempt(d1) is False
        clock.advance(11)
        d2 = await deliveries.get(TENANT, row.id)
        assert d2 is not None
        assert await service.attempt(d2) is True
    # then the secret value appears in no log record ...
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args)
    # ... in no audit payload ...
    for entry in audit.entries:
        assert SECRET not in str(entry.get("payload"))
    # ... in no stored delivery field ...
    stored = await deliveries.get(TENANT, row.id)
    assert stored is not None
    assert SECRET not in str(stored.last_error)
    assert SECRET not in str(stored.target_url)
    # ... and in no wire header (signatures are digests, not the secret)
    for call in transport.calls:
        for value in call["headers"].values():
            assert SECRET not in value
