"""Catalog subscriber factory mirroring domain events to outbound subscribers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.context import Clock
from app.domain.ports import (
    AuditEventStore,
    OutboundDeliveryRepository,
    WebhookSubscriptionRepository,
)

from .constants import logger
from .enqueue import EnqueueDeliveryCommand, enqueue_delivery


def make_catalog_subscriber(
    *,
    delivery_service_factory: Any,
    subscription_repo: WebhookSubscriptionRepository,
    delivery_repo: OutboundDeliveryRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> Any:
    """Build a MessageBus subscriber that mirrors events to outbound subs.

    The subscriber is registered with the local ``MessageBus`` at
    startup. For every event drained from the outbox, it:

    1. Looks up subscriptions for the tenant whose filter set
       includes the event_type.
    2. Enqueues a delivery row per (subscription, event) pair.
       The enqueue is idempotent on ``(subscription_id, event_id)``
       so a bus-level replay does not double-enqueue.
    3. Hands the rows to the ``DeliveryService`` to dispatch
       immediately (a sync enqueue+dispatch keeps the in-process
       latency low; in production the dispatcher runs as a
       separate worker).

    The factory is supplied rather than the service directly so
    the test suite can substitute a service whose transport is a
    recording stub. The factory must return a :class:`DeliveryService`.
    """

    async def _subscriber(event: dict[str, Any]) -> None:
        tenant_id = UUID(event["tenant_id"])
        event_id = UUID(event["event_id"])
        event_type = event["event_type"]
        # Snapshot subscriptions at dispatch time. We do not
        # cache because subscriptions are operator-managed; a
        # "create subscription" must take effect on the next
        # event, not after a TTL.
        subs = await subscription_repo.list(tenant_id, limit=200)
        # Filter in-process: matches_event is a one-shot check
        # that also enforces the ACTIVE status.
        matching = [s for s in subs if s.matches_event(event_type)]
        if not matching:
            return
        service = delivery_service_factory()
        for sub in matching:
            try:
                delivery, created = await enqueue_delivery(
                    EnqueueDeliveryCommand(
                        tenant_id=tenant_id,
                        subscription=sub,
                        event_id=event_id,
                        event_type=event_type,
                        payload=event.get("payload", {}),
                        correlation_id=event.get("correlation_id"),
                        occurred_at=event.get("occurred_at"),
                    ),
                    delivery_repo=delivery_repo,
                    audit=audit,
                    clock=clock,
                )
            except Exception:  # noqa: BLE001 — let the outbox retry failed enqueues
                logger.exception(
                    "outbound_webhook.enqueue_failed",
                    extra={
                        "subscription_id": str(sub.id),
                        "event_id": str(event_id),
                        "event_type": event_type,
                    },
                )
                # The outbox is the only durable retry source before a
                # delivery row exists. Propagate enqueue failures so its
                # dispatcher leaves this event pending instead of acking it.
                raise
            if not created:
                # A prior delivery exists; the dispatcher loop
                # owns its retry policy. We do not re-dispatch
                # here because the prior row's attempt counter
                # carries the retry budget.
                continue
            # Dispatch inline. The transport applies a per-attempt
            # timeout; a slow subscriber cannot block the bus for
            # longer than that.
            try:
                await service.attempt(delivery)
            except Exception:  # noqa: BLE001 — same as above
                logger.exception(
                    "outbound_webhook.dispatch_failed",
                    extra={
                        "delivery_id": str(delivery.id),
                        "subscription_id": str(sub.id),
                    },
                )

    return _subscriber


__all__ = [
    "make_catalog_subscriber",
]
