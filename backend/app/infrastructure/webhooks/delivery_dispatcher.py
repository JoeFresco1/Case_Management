"""Outbound webhook delivery dispatcher: drains pending delivery rows per tenant.

# INTEGRATION_SEAM: webhook delivery dispatcher — local in-process loop;
production target is a hosted worker (Azure Functions / Container Apps)
consuming the same ``outbound_deliveries`` table via the same dispatcher
logic. The dispatcher itself is the same code path in both postures —
only the scheduler differs.

Why a dispatcher at all (FM-056 / P13-US10): the delivery row is written
in the same DB transaction as the enqueue (or by the ``MessageBus``
catalog subscriber). A separate worker drains pending rows after commit
so that:

- A subscriber outage cannot lose the delivery intent (the row is
  already durable).
- A duplicate POST on retry is harmless because subscribers dedupe on
  the ``X-Webhook-Event-Id`` header (the row carries a stable
  ``event_id``).
- The dispatcher is the single integration point — the replay endpoint
  (``POST /api/v1/webhooks/deliveries/{id}/replay``) only resets the
  row to ``PENDING``; the next dispatcher tick re-attempts it.

The loop mirrors :class:`OutboxDispatcher
<app.infrastructure.messaging.outbox_dispatcher.OutboxDispatcher>`:
``tick()`` runs forever until cancelled and never dies on an exception;
``drain_once()`` enumerates tenants, builds one ``DeliveryService`` per
tenant from session-bound SQL repositories plus the transport, and calls
``dispatch_pending``. One tenant's failure never starves the others.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.webhooks.outbound import MAX_ATTEMPTS, DeliveryService
from app.domain.context import Clock
from app.domain.ports import OutboundDeliveryTransport, SecretProvider
from app.infrastructure.persistence.models import Tenant
from app.infrastructure.persistence.repositories import (
    SqlOutboundDeliveryRepository,
    SqlWebhookSubscriptionRepository,
)
from app.infrastructure.webhooks.httpx_transport import HttpXOutboundTransport

logger = logging.getLogger(__name__)


# why: a module-level alias keeps the constructor signature readable and
# gives the main.py owner a named type for custom tenant sources (e.g. a
# cached tenant list in production). The default source is the portable
# ``SELECT id FROM tenants`` query below.
TenantIdsProvider = Callable[[], Awaitable[list[UUID]]]

# why: the injected factory owns the whole per-tenant service (repos +
# transport + secrets + clock), so dependency-free unit tests can supply
# in-memory fakes without a database. The factory takes only the tenant
# id; the dispatcher never inspects the service internals beyond calling
# ``dispatch_pending``.
DeliveryServiceFactory = Callable[[UUID], DeliveryService]

# why: a factory (rather than a shared instance) lets the main.py owner
# scope transports per tick — e.g. one ``httpx.AsyncClient`` per worker
# process, or a fresh recording transport per test. The default factory
# returns a single shared ``HttpXOutboundTransport`` (connection-pooled
# by httpx, safe to share across tenants).
TransportFactory = Callable[[], OutboundDeliveryTransport]


class _SystemClock:
    """Production clock supplied at the infrastructure boundary.

    Why a private clock rather than importing one: the domain ``Clock``
    Protocol is structural, so any object with ``now()`` satisfies it.
    Keeping the implementation here (mirroring ``SqlSlaScanner``) keeps
    the dispatcher self-contained; tests inject a fixed clock.
    """

    def now(self) -> datetime:
        """Return the current UTC timestamp."""
        return datetime.now(tz=UTC)


class WebhookDeliveryDispatcher:
    """Drains pending outbound webhook deliveries for every tenant.

    Lifecycle: construct once at startup with a session factory, a
    transport (or transport factory), a secret provider, and a clock;
    call ``tick`` on an interval (driven by the FastAPI startup task in
    ``main.py``) or ``drain_once`` from a CLI / one-shot test. ``tick``
    runs forever until cancelled.

    Args:
        sessionmaker: SQLAlchemy session factory bound to the app
            engine. May be ``None`` only when both
            ``tenant_ids_provider`` and ``service_factory`` are
            injected (dependency-free unit tests); the SQL-backed paths
            fail closed otherwise.
        transport: Shared transport used for every tenant drain.
            Mutually exclusive with ``transport_factory``.
        transport_factory: Called once per tenant drain to resolve the
            transport. Wins over ``transport`` when both are absent;
            passing both raises ``ValueError``. Defaults to a shared
            ``HttpXOutboundTransport``.
        secret_provider: Resolves per-subscription signing secrets.
            Required for the default SQL-backed service path (fail
            closed: a missing provider raises at drain time rather than
            dead-lettering every row with ``missing_secret``). Unused
            when ``service_factory`` is injected.
        clock: Clock for delivery timestamps. Defaults to the system
            clock. Unused when ``service_factory`` is injected.
        sleep_seconds: Delay between ``tick`` iterations.
        max_attempts: Retry cap forwarded to each default-built
            ``DeliveryService``. Unused when ``service_factory`` is
            injected (the factory owns the cap).
        tenant_ids_provider: Async callable returning all tenant ids.
            Defaults to the portable ``SELECT id FROM tenants`` query.
        service_factory: Builds a ready ``DeliveryService`` for a
            tenant id. Defaults to session-bound SQL repositories plus
            the configured transport / secrets / clock. Inject fakes
            here for database-free unit tests.

    Failure mode: ``tick`` never dies on an exception (it logs and
    sleeps before the next iteration); ``drain_once`` isolates tenants
    (one tenant's failure is logged and the loop continues); per-tenant
    work commits on success and rolls back on error so a partial tenant
    drain never persists half-attempted rows.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        transport: OutboundDeliveryTransport | None = None,
        transport_factory: TransportFactory | None = None,
        secret_provider: SecretProvider | None = None,
        clock: Clock | None = None,
        sleep_seconds: float = 5.0,
        max_attempts: int = MAX_ATTEMPTS,
        tenant_ids_provider: TenantIdsProvider | None = None,
        service_factory: DeliveryServiceFactory | None = None,
    ) -> None:
        if transport is not None and transport_factory is not None:
            # why fail closed: two transport sources is a wiring bug;
            # silently preferring one would hide the misconfiguration
            # until a subscriber reports missing deliveries.
            raise ValueError("pass at most one of transport / transport_factory")
        self._sm = sessionmaker
        if transport_factory is not None:
            self._transport_factory = transport_factory
        elif transport is not None:
            self._transport_factory: TransportFactory = lambda: transport
        else:
            # why shared: httpx connection-pools internally, so one
            # instance serves all tenants without per-drain setup cost.
            # The main.py owner may pass a factory for per-tick scoping.
            shared = HttpXOutboundTransport()
            self._transport_factory = lambda: shared
        self._secrets = secret_provider
        self._clock: Clock = clock if clock is not None else _SystemClock()
        self._sleep_seconds = sleep_seconds
        self._max_attempts = max_attempts
        self._tenant_ids_provider = tenant_ids_provider
        self._service_factory = service_factory

    async def tick(self) -> None:
        """Run ``drain_once`` on an interval until cancelled.

        Why a separate tick loop rather than a background asyncio task
        in ``main``: keeping the loop in this class makes the dispatcher
        testable in isolation (tests call ``drain_once`` directly, or
        drive ``tick`` briefly and cancel) and keeps ``main`` from
        owning a long-running task it cannot cleanly cancel on shutdown.
        A failed drain is logged and retried on the next iteration; the
        loop only exits on ``CancelledError``.
        """
        try:
            while True:
                try:
                    await self.drain_once()
                except Exception:  # noqa: BLE001 — loop must not die
                    logger.exception("webhook delivery tick failed")
                await asyncio.sleep(self._sleep_seconds)
        except asyncio.CancelledError:
            return

    async def drain_once(self, *, batch_size: int = 50) -> int:
        """Drain pending deliveries for every tenant; return terminal count.

        "Terminal" here means the row reached ``SUCCEEDED`` or
        ``DEAD_LETTERED`` (see ``DeliveryService.dispatch_pending``).
        Rows skipped because their subscription is disabled/retired, and
        rows that hit a retriable failure, stay ``PENDING`` and are not
        counted — the next tick retries them.

        Args:
            batch_size: Maximum pending rows attempted per tenant. Why
                a per-tenant cap rather than a global one: a global cap
                would let one tenant's backlog starve every other tenant
                behind it in enumeration order.

        Returns:
            Total terminal deliveries across all tenants.

        Failure mode: tenant enumeration errors propagate (``tick``
        catches and retries next interval); per-tenant drain errors are
        logged and skipped so one tenant never starves the others.
        """
        tenant_ids = await self._tenant_ids()
        total = 0
        for tenant_id in tenant_ids:
            try:
                total += await self._drain_tenant(tenant_id, batch_size=batch_size)
            except Exception:  # noqa: BLE001 — one tenant must not starve others
                logger.exception(
                    "webhook delivery drain failed",
                    extra={"tenant_id": str(tenant_id)},
                )
        return total

    async def _tenant_ids(self) -> list[UUID]:
        """Return all tenant ids via the injected provider or the SQL default.

        Failure mode: raises when the sessionmaker is missing and no
        provider was injected (fail closed — silently draining zero
        tenants would hide the wiring bug until deliveries pile up).
        """
        if self._tenant_ids_provider is not None:
            return await self._tenant_ids_provider()
        if self._sm is None:
            raise RuntimeError(
                "WebhookDeliveryDispatcher has no sessionmaker and no "
                "tenant_ids_provider: pass sessionmaker= (production) or "
                "tenant_ids_provider= (tests)."
            )
        async with self._sm() as session:
            # why ordered: deterministic enumeration keeps multi-tenant
            # drain logs comparable across ticks.
            result = await session.execute(select(Tenant.id).order_by(Tenant.id.asc()))
            return list(result.scalars().all())

    async def _drain_tenant(self, tenant_id: UUID, *, batch_size: int) -> int:
        """Drain one tenant's pending rows; return the terminal count.

        The injected ``service_factory`` path (unit tests) delegates
        entirely to the fake service — no session, no commit. The
        default SQL path builds session-bound repositories, dispatches,
        and commits atomically; on error the transaction rolls back and
        the exception propagates to ``drain_once`` for per-tenant
        isolation logging.
        """
        if self._service_factory is not None:
            service = self._service_factory(tenant_id)
            return await service.dispatch_pending(tenant_id=tenant_id, batch_size=batch_size)
        if self._sm is None:
            raise RuntimeError(
                "WebhookDeliveryDispatcher has no sessionmaker and no "
                "service_factory: pass sessionmaker= (production) or "
                "service_factory= (tests)."
            )
        if self._secrets is None:
            # why fail closed here rather than letting every row
            # dead-letter with missing_secret: a missing provider is a
            # wiring bug, and per-row dead-letters would destroy the
            # retry budget for rows that were never actually attempted.
            raise RuntimeError(
                "WebhookDeliveryDispatcher has no secret_provider: the main.py "
                "owner must pass secret_provider=app.state.secret_provider."
            )
        async with self._sm() as session:
            try:
                service = DeliveryService(
                    delivery_repo=SqlOutboundDeliveryRepository(session),
                    subscription_repo=SqlWebhookSubscriptionRepository(session),
                    transport=self._transport_factory(),
                    secret_provider=self._secrets,
                    clock=self._clock,
                    max_attempts=self._max_attempts,
                )
                dispatched = await service.dispatch_pending(
                    tenant_id=tenant_id, batch_size=batch_size
                )
                await session.commit()
                return dispatched
            except Exception:
                await session.rollback()
                raise


__all__ = [
    "DeliveryServiceFactory",
    "TenantIdsProvider",
    "TransportFactory",
    "WebhookDeliveryDispatcher",
]
