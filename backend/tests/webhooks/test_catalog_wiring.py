"""Composition-root tests for outbound webhook catalog delivery wiring."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.outbound_webhook import (
    DeliveryState,
    SigningAlgorithm,
    SubscriptionStatus,
    WebhookSubscription,
)
from app.domain.ports import TransportResult
from app.infrastructure.messaging.local_message_bus import LocalInProcessMessageBus
from app.infrastructure.persistence.models import (
    OutboundDelivery as OutboundDeliveryRow,
)
from app.infrastructure.persistence.models import Tenant
from app.infrastructure.persistence.repositories import SqlWebhookSubscriptionRepository
from app.infrastructure.webhooks.delivery_dispatcher import WebhookDeliveryDispatcher
from app.infrastructure.webhooks.httpx_transport import RecordingOutboundTransport


class _TestSecretProvider:
    """Return one synthetic signing secret for the registered test subscription."""

    async def get(self, name: str) -> str:
        if name != "catalog-test-secret":
            raise KeyError(name)
        return "catalog-test-signing-secret-value"

    async def set_secret(self, name: str, value: str) -> None:
        return None


@pytest.mark.asyncio
async def test_webhook_catalog_wiring__failed_post__persists_and_retries_delivery(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The outbox bus subscriber persists failed sends for dispatcher retry."""
    # FastAPI currently warns while main.py constructs the application at import.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from app.main import _register_webhook_catalog_subscriber  # noqa: PLC0415

    tenant_id = uuid4()
    event_id = uuid4()
    subscription_id = uuid4()
    now = datetime.now(tz=UTC)
    transport = RecordingOutboundTransport()
    transport.set_response(
        TransportResult(success=False, status_code=503, error="upstream unavailable")
    )
    bus = LocalInProcessMessageBus()

    async with sql_session_factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                key="catalog-test",
                name="Catalog Test Tenant",
                created_at=now,
            )
        )
        await session.flush()
        subscription = WebhookSubscription(
            id=subscription_id,
            tenant_id=tenant_id,
            url="https://example.test/webhooks/case-created",
            event_filters=("CaseCreated",),
            secret_name="catalog-test-secret",
            signing_algorithm=SigningAlgorithm.HMAC_SHA256,
            status=SubscriptionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        await SqlWebhookSubscriptionRepository(session).add(subscription)
        await session.commit()

    secrets = _TestSecretProvider()
    await _register_webhook_catalog_subscriber(
        bus,
        sessionmaker=sql_session_factory,
        secret_provider=secrets,
        transport=transport,
    )

    await bus.publish(
        tenant_id=tenant_id,
        event_type="CaseCreated",
        event_id=event_id,
        payload={"case_id": str(uuid4())},
        correlation_id="catalog-test-correlation",
        occurred_at=now.isoformat(),
    )

    async with sql_session_factory() as session:
        row = (
            await session.execute(
                select(OutboundDeliveryRow).where(
                    OutboundDeliveryRow.tenant_id == tenant_id,
                    OutboundDeliveryRow.subscription_id == subscription_id,
                    OutboundDeliveryRow.event_id == event_id,
                )
            )
        ).scalar_one()
        assert row.state == DeliveryState.PENDING.value
        assert row.attempt_count == 1
        assert row.last_status_code == 503
        row.last_attempted_at = now - timedelta(minutes=1)
        await session.commit()

    # A repeated outbox delivery is idempotent; the durable row owns retries.
    await bus.publish(
        tenant_id=tenant_id,
        event_type="CaseCreated",
        event_id=event_id,
        payload={"case_id": "replayed"},
        correlation_id="catalog-test-correlation",
        occurred_at=now.isoformat(),
    )
    transport.set_response(TransportResult(success=True, status_code=204))
    dispatcher = WebhookDeliveryDispatcher(
        sessionmaker=sql_session_factory,
        transport=transport,
        secret_provider=secrets,
        sleep_seconds=0.01,
    )

    await dispatcher.drain_once()

    async with sql_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(OutboundDeliveryRow).where(
                        OutboundDeliveryRow.tenant_id == tenant_id,
                        OutboundDeliveryRow.subscription_id == subscription_id,
                        OutboundDeliveryRow.event_id == event_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].state == DeliveryState.SUCCEEDED.value
        assert rows[0].attempt_count == 2
    assert len(transport.calls) == 2
