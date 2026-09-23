"""Tests for SLA scheduler cadence and lifecycle behavior."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.application.sla.scheduler import SlaScannerScheduler


@pytest.mark.asyncio
async def test_sla_scheduler_run_once__isolates_tenant_failure() -> None:
    """A failed tenant does not prevent later tenants from being scanned."""
    tenant_ids = [uuid4(), uuid4()]
    visited: list[object] = []

    async def tenants() -> list[object]:
        return tenant_ids

    async def scan(tenant_id: object) -> None:
        visited.append(tenant_id)
        if tenant_id == tenant_ids[0]:
            raise RuntimeError("temporary storage outage")

    scheduler = SlaScannerScheduler(tenants=tenants, scan_tenant=scan)

    assert await scheduler.run_once() == 1
    assert visited == tenant_ids


@pytest.mark.asyncio
async def test_sla_scheduler_lifecycle__stop_is_idempotent() -> None:
    """Startup creates one task and repeated shutdown does not leak it."""
    calls = 0

    async def tenants() -> list[object]:
        return []

    async def scan(_: object) -> None:
        nonlocal calls
        calls += 1

    scheduler = SlaScannerScheduler(
        tenants=tenants,
        scan_tenant=scan,
        interval_seconds=60,
    )
    task = scheduler.start()
    assert scheduler.start() is task
    await scheduler.stop()
    await scheduler.stop()
    await asyncio.sleep(0)
    assert task.done()
    assert calls == 0


def test_sla_scheduler_constructor__rejects_non_positive_interval() -> None:
    """A non-positive cadence would create a hot loop and is invalid."""

    async def tenants() -> list[object]:
        return []

    async def scan(_: object) -> None:
        return None

    with pytest.raises(ValueError, match="positive"):
        SlaScannerScheduler(tenants=tenants, scan_tenant=scan, interval_seconds=0)
