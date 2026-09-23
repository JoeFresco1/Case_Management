"""Delivery enqueue: idempotent insert of one outbound delivery row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.domain.context import Clock
from app.domain.errors import Conflict, ValidationFailed
from app.domain.events import find_event_spec
from app.domain.outbound_webhook import DeliveryState, OutboundDelivery, WebhookSubscription
from app.domain.ports import AuditEventStore, OutboundDeliveryRepository

from .audit import _record_audit


@dataclass(frozen=True, slots=True)
class EnqueueDeliveryCommand:
    """Input to :func:`enqueue_delivery`.

    The ``MessageBus`` subscriber (wired in ``app.main``) builds one
    of these per (subscription, event) pair when a domain event is
    dispatched from the outbox.
    """

    tenant_id: UUID
    subscription: WebhookSubscription
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    correlation_id: str | None = None
    occurred_at: str | None = None


async def enqueue_delivery(
    cmd: EnqueueDeliveryCommand,
    *,
    delivery_repo: OutboundDeliveryRepository,
    audit: AuditEventStore,
    clock: Clock,
) -> tuple[OutboundDelivery, bool]:
    """Enqueue an outbound delivery for one (subscription, event) pair.

    Returns ``(delivery, created)`` where ``created=True`` means a
    new row was inserted and ``created=False`` means a prior row
    was found and returned unchanged. The application layer
    translates ``created=False`` to a no-op (the dispatcher will
    retry the prior row's pending attempts).

    Idempotency is enforced at the database level: the unique
    constraint ``(tenant_id, subscription_id, event_id)`` fires on
    the loser's ``INSERT``; the implementation translates the
    ``IntegrityError`` to a follow-up ``get_by_event`` lookup.

    The function refuses to enqueue for a subscription that does not
    match the event_type — the caller is expected to have already
    filtered, but the guard is defense-in-depth.
    """
    if not cmd.subscription.matches_event(cmd.event_type):
        # Fail-closed: a subscription whose filter does not include
        # this event_type MUST NOT receive a delivery row. The bus
        # subscriber does the filter; this guard catches a caller
        # that forgot.
        raise ValidationFailed(
            f"subscription {cmd.subscription.id} does not match event_type {cmd.event_type!r}",
            field="subscription_id",
        )
    if find_event_spec(cmd.event_type) is None:
        # Defense-in-depth: a known event_type was the precondition
        # for the catalog subscriber; this guard catches a misuse
        # from a non-catalog caller.
        raise ValidationFailed(
            f"event_type {cmd.event_type!r} is not in the catalog",
            field="event_type",
        )
    # Pre-check: a prior row means we are replaying. The dispatcher
    # owns the retry policy; the enqueue is a no-op.
    prior = await delivery_repo.get_by_event(cmd.tenant_id, cmd.subscription.id, cmd.event_id)
    if prior is not None:
        return prior, False
    delivery = OutboundDelivery(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        subscription_id=cmd.subscription.id,
        event_id=cmd.event_id,
        event_type=cmd.event_type,
        payload_json=dict(cmd.payload),
        correlation_id=cmd.correlation_id,
        occurred_at=cmd.occurred_at,
        target_url=cmd.subscription.url,
        state=DeliveryState.PENDING,
        attempt_count=0,
        last_status_code=None,
        last_error=None,
        first_attempted_at=None,
        last_attempted_at=None,
        succeeded_at=None,
        created_at=clock.now(),
    )
    try:
        await delivery_repo.add(delivery)
    except Conflict:
        # Concurrent enqueue: the other writer won. Re-read and
        # return the prior row. The dispatcher will retry whichever
        # row is pending.
        prior = await delivery_repo.get_by_event(cmd.tenant_id, cmd.subscription.id, cmd.event_id)
        if prior is None:
            # Should not happen: Conflict fired but the row is
            # not visible. Re-raise to surface the bug.
            raise
        return prior, False
    await _record_audit(
        audit,
        tenant_id=cmd.tenant_id,
        event_type="OutboundDeliveryEnqueued",
        actor_id=None,
        subject_id=delivery.id,
        correlation_id=cmd.correlation_id,
        payload={
            "subscription_id": str(cmd.subscription.id),
            "event_id": str(cmd.event_id),
            "event_type": cmd.event_type,
        },
        clock=clock,
    )
    return delivery, True


__all__ = [
    "Any",
    "DeliveryState",
    "EnqueueDeliveryCommand",
    "OutboundDelivery",
    "OutboundDeliveryRepository",
    "enqueue_delivery",
]
