"""Outbound webhook ports (FM-056 / P13-US10 / ADR-013).

These are the OUTBOUND half of the webhook story. They are deliberately
separate from CapturedWebhookRepository (the INBOUND half, FM-030 /
ADR-007): trust direction, idempotency anchor, and failure semantics
are all inverted. See 01-deduplicated-feature-map.md:189 and
``app.domain.outbound_webhook`` for the contrast.

Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
takes ``tenant_id`` first; the SQL implementation must include
``WHERE tenant_id = :tenant_id``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.outbound_webhook import OutboundDelivery, WebhookSubscription
from app.domain.ports.core import TenantScoped


class WebhookSubscriptionRepository(TenantScoped, Protocol):
    """Outbound webhook subscription persistence (FM-056).

    A subscription is the authorization anchor for outbound delivery:
    a delivery is permitted only if the subscription is ACTIVE and the
    event_type is in the subscription's filter set. The repository
    methods are tenant-scoped so a cross-tenant subscription id is
    never observable.
    """

    async def get(self, tenant_id: UUID, subscription_id: UUID) -> WebhookSubscription | None:
        """Return the subscription, or ``None``.

        Returns ``None`` for a missing subscription AND for a
        subscription in a different tenant; the API surface must not
        be able to distinguish (Constitution V).
        """

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[WebhookSubscription]:
        """List subscriptions for the tenant, newest first."""

    async def add(self, subscription: WebhookSubscription) -> None:
        """Persist a new subscription.

        Implementations MUST insert the row in the same transaction as
        the audit row so a partial commit never leaves a subscription
        without provenance (Constitution V).
        """

    async def update(self, subscription: WebhookSubscription) -> None:
        """Persist changes to an existing subscription.

        Implementations MUST enforce optimistic concurrency on
        ``version_token`` (a stale token raises ``Conflict``). The
        caller is expected to have already bumped ``version_token``
        via :meth:`WebhookSubscription.bump_version`.
        """


class OutboundDeliveryRepository(TenantScoped, Protocol):
    """Outbound webhook delivery persistence (FM-056).

    The delivery row is the durable record of "the platform tried to
    send event X to subscription Y". It is the retry anchor: the
    dispatcher reads pending rows, attempts the HTTP POST, and either
    marks the row succeeded or bumps the attempt counter.

    Idempotency is enforced at the database level: the unique
    constraint ``(tenant_id, subscription_id, event_id)`` ensures a
    second enqueue for the same event is a no-op. The application
    layer catches the ``IntegrityError`` and returns the prior row.
    """

    async def get(self, tenant_id: UUID, delivery_id: UUID) -> OutboundDelivery | None:
        """Return the delivery, or ``None`` (cross-tenant = None)."""

    async def get_by_event(
        self, tenant_id: UUID, subscription_id: UUID, event_id: UUID
    ) -> OutboundDelivery | None:
        """Return the prior delivery for this (sub, event) pair.

        Used as the pre-check for content idempotency: a hit means
        "this event has already been delivered (or is in flight) to
        this subscription." The platform does not re-enqueue.
        """

    async def list_pending(
        self,
        tenant_id: UUID,
        *,
        subscription_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[OutboundDelivery]:
        """Return pending deliveries, oldest first.

        The dispatcher calls this to drive the retry loop. The optional
        ``subscription_id`` filter is used by the operator console
        ("show me what's queued for this endpoint").
        """

    async def add(self, delivery: OutboundDelivery) -> None:
        """Persist a new delivery row.

        Implementations MUST enforce the
        ``UNIQUE (tenant_id, subscription_id, event_id)`` constraint
        and surface the violation as a domain ``Conflict`` (or an
        ``IntegrityError`` the application layer can translate to
        ``Conflict``).
        """

    async def update(self, delivery: OutboundDelivery) -> None:
        """Persist changes to an existing delivery row."""

    async def list_dead_lettered(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
    ) -> Sequence[OutboundDelivery]:
        """Return dead-lettered deliveries for the operator console.

        The operator can replay these via the admin endpoint, which
        resets the row to ``PENDING`` and clears ``attempt_count``.
        """


class OutboundDeliveryTransport(Protocol):
    """The HTTP transport seam for outbound deliveries (FM-056).

    The application layer is responsible for **constructing the
    request**: signing the body, attaching the JWT bearer (if
    required), setting the timestamp, etc. The transport's only
    responsibility is to **POST** and return the outcome. Keeping the
    seam narrow lets the test suite substitute a recording transport
    and lets the production adapter be a thin wrapper around
    ``httpx.AsyncClient`` (or any other HTTP client).

    Implementations MUST:

    - Apply a per-call wall-clock timeout (the application layer
      supplies ``timeout_seconds``).
    - Not retry on the caller's behalf -- the application layer owns
      the retry policy.
    - Return a :class:`TransportResult` with a numeric status code
      (or ``None`` if the call failed before reaching the wire),
      the truncated error string, and a boolean ``success`` flag.
    """

    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> TransportResult: ...


@dataclass(frozen=True, slots=True)
class TransportResult:
    """The outcome of one ``OutboundDeliveryTransport.post`` call.

    Attributes:
        success: ``True`` for HTTP 2xx; ``False`` for any other
            status, transport error, or timeout.
        status_code: The HTTP status returned by the subscriber
            (``None`` if no response was received).
        error: Truncated error string (``None`` on success or when
            the transport has no error to report). The application
            layer is responsible for truncation; this is the
            transport's verbatim string.
    """

    success: bool
    status_code: int | None = None
    error: str | None = None


__all__ = [
    "OutboundDeliveryRepository",
    "OutboundDeliveryTransport",
    "TransportResult",
    "WebhookSubscriptionRepository",
]
