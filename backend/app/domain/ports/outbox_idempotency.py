"""Outbox and Idempotency ports."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped


class OutboxRecord(Protocol):
    pass


class OutboxWriter(Protocol):
    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None: ...


class IdempotencyStore(TenantScoped, Protocol):
    """Durable store for `Idempotency-Key` replays.

    Used by `interfaces/http/idempotency.py`. Implementations MUST scope
    lookups by `tenant_id` so a key in tenant A cannot match a request in
    tenant B.
    """

    async def get(self, *, tenant_id: UUID, key: str) -> dict[str, Any] | None:
        """Return a previously stored response, or None."""

    async def put(
        self,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        correlation_id: str,
    ) -> None:
        """Record a successful response for replay."""


__all__ = [
    "IdempotencyStore",
    "OutboxRecord",
    "OutboxWriter",
]
