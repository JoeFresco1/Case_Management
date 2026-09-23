"""Local in-process MessageBus implementation.

# INTEGRATION_SEAM: MessageBus — ``LocalInProcessMessageBus`` is the local
adapter; the production seam is ``AzureServiceBus`` (publisher + topic
subscription). The port is in ``app.domain.ports.MessageBus``.

This adapter is intentionally tiny: it accepts publishes from the outbox
dispatcher and fans them out to a list of in-process subscribers. The
adapter is **not** the durable transport — the outbox row is the
durable record of truth. A subscriber exception is propagated to the
caller so the dispatcher can mark the row as failed and retry on the
next tick; the row is never lost (AGENTS.md §12, plan §4.R12).

Why propagate (instead of swallow): the in-process bus has no other
delivery guarantee. If a subscriber raises, the only way the event is
ever delivered is by the dispatcher retrying the row, which only happens
when publish raises. The production Azure adapter delegates delivery
guarantees to the Service Bus itself; the local adapter must enforce
the same contract.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.domain.events import find_event_spec

logger = logging.getLogger(__name__)


# Subscriber callback signature. The bus invokes the callback with the
# wire-shaped event dict (event_type, event_id, tenant_id, payload,
# correlation_id, causation_id, occurred_at). Subscribers MUST be
# idempotent — the outbox may replay after a transient failure.
Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


class LocalInProcessMessageBus:
    """In-process fan-out for events drained from the outbox.

    Why a subscriber list rather than a topic map: the catalog has
    fewer than ten event types (FM-055); a topic-based router is the
    Azure adapter's job. Local subscribers register a callback and
    either filter inside the callback or wrap with the
    ``subscribe_to`` helper below.

    Thread safety: subscriptions and publish are protected by a lock
    so a subscriber registering during dispatch does not race with
    the iteration. The bus is awaitable; ``publish`` is awaited so a
    slow subscriber does not block the dispatcher tick indefinitely —
    the dispatcher wraps publish in ``asyncio.wait_for`` to bound
    wall-clock latency.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = asyncio.Lock()

    async def subscribe(self, callback: Subscriber) -> None:
        """Register ``callback`` to receive every published event."""
        async with self._lock:
            self._subscribers.append(callback)

    def subscribe_to(self, event_type: str) -> Callable[[Subscriber], Subscriber]:
        """Decorator factory: register a callback for a single event_type.

        Usage::

            @bus.subscribe_to("CaseCreated")
            async def _on_case_created(event):
                ...

        Why a decorator: makes the (event_type -> handler) binding
        local to the handler, which mirrors the Azure adapter's topic
        subscription shape (one topic per event_type).
        """

        def decorator(fn: Subscriber) -> Subscriber:
            async def wrapper(event: dict[str, Any]) -> None:
                if event.get("event_type") == event_type:
                    await fn(event)

            wrapper.__name__ = fn.__name__
            wrapper.__doc__ = fn.__doc__
            self._subscribers.append(wrapper)
            return wrapper

        return decorator

    async def publish(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        """Fan out ``event_type`` to every registered subscriber.

        The dispatcher calls this with the wire-shaped kwargs from the
        outbox row. The wire envelope below is what subscribers see;
        keep the shape stable — it is the integration contract
        (AGENTS.md §16.1, plan §17.1).

        Raises the first subscriber exception after attempting every
        subscriber. The local adapter has no other delivery
        guarantee, so a failure must propagate so the dispatcher can
        mark the outbox row as failed and retry.
        """
        wire = {
            "event_type": event_type,
            "event_id": str(event_id),
            "tenant_id": str(tenant_id),
            "payload": dict(payload),
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "occurred_at": occurred_at,
            # Schema version is surfaced so subscribers can negotiate
            # on it. The catalog is the source of truth; the bus does
            # not enforce a version match (the catalog does, at
            # construction).
            "version": (
                find_event_spec(event_type).version
                if find_event_spec(event_type) is not None
                else 0
            ),
        }
        # Snapshot subscribers under the lock so a concurrent subscribe
        # does not race with the iteration.
        async with self._lock:
            targets = list(self._subscribers)
        first_exc: BaseException | None = None
        for cb in targets:
            try:
                await cb(wire)
            except Exception as exc:  # noqa: BLE001 — propagate for retry
                logger.exception(
                    "subscriber failed",
                    extra={"event_type": event_type, "event_id": str(event_id)},
                )
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            # Re-raise so the dispatcher marks the row as failed.
            # ``raise from None`` to avoid chaining through asyncio
            # internals; the bus has already logged the full traceback.
            raise first_exc


__all__ = ["LocalInProcessMessageBus", "Subscriber"]
