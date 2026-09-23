"""Reliable scheduling seam for the SLA scanner.

The scheduler owns only cadence and lifecycle.  The injected callbacks own
tenant discovery and persistence, which keeps the deterministic scanner
usable from tests and from a future hosted worker (Azure Functions /
Container Apps) without changing business rules.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from uuid import UUID

logger = logging.getLogger(__name__)

TenantProvider = Callable[[], Awaitable[Sequence[UUID]]]
TenantScan = Callable[[UUID], Awaitable[None]]


class SlaScannerScheduler:
    """Run one SLA scan per tenant on a cancellable fixed interval.

    ``run_once`` is the deterministic entry point for tests and one-shot
    workers.  A tenant failure is isolated and logged; the next tenant and
    next tick still run.  ``start`` and ``stop`` are idempotent so application
    lifespan hooks can safely be re-entered by test clients.
    """

    def __init__(
        self,
        *,
        tenants: TenantProvider,
        scan_tenant: TenantScan,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("SLA scan interval must be positive")
        self._tenants = tenants
        self._scan_tenant = scan_tenant
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        """Return the active background task, if started."""
        return self._task

    async def run_once(self) -> int:
        """Scan all currently known tenants and return successful count.

        Tenant discovery errors are logged and re-raised because no safe
        tenant scope can be established.  Individual scan errors are logged
        and isolated so one tenant cannot starve the remainder.
        """
        tenant_ids = await self._tenants()
        successful = 0
        for tenant_id in tenant_ids:
            try:
                await self._scan_tenant(tenant_id)
            except Exception:  # noqa: BLE001 - worker must survive one tenant
                logger.exception("sla scan failed", extra={"tenant_id": str(tenant_id)})
            else:
                successful += 1
        return successful

    def start(self) -> asyncio.Task[None]:
        """Start the background loop once and return its task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="sla-scanner")
        return self._task

    async def stop(self) -> None:
        """Cancel and await the background loop without leaking tasks."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_forever(self) -> None:
        """Run scans every interval until cancellation.

        The first scan is delayed by one interval so application startup is
        not held hostage by a database outage; ``run_once`` remains available
        for deterministic immediate execution.
        """
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 - discovery failure is retryable
                    logger.exception("sla tenant discovery failed")
        except asyncio.CancelledError:
            return


__all__ = ["SlaScannerScheduler"]
