"""Infrastructure-free idempotency provider contract for HTTP middleware."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID


class IdempotencyTransaction(Protocol):
    """Own one mutation session and its durable response reservation."""

    session: object
    prior: dict[str, object] | None

    async def finalize(
        self, *, response_status: int, response_body: object, correlation_id: str
    ) -> None:
        """Commit the mutation and response together; raise on persistence failure."""
        ...


class IdempotencyHttpDependencyProvider(Protocol):
    """Reserve a key and own the transaction through response serialization."""

    def transaction(
        self, *, tenant_id: UUID, key: str, request_hash: str, correlation_id: str
    ) -> AbstractAsyncContextManager[IdempotencyTransaction]:
        """Yield a reservation; roll back unfinished work on failure or cancellation."""
        ...

    async def get(self, *, tenant_id: UUID, key: str) -> dict[str, object] | None:
        """Return a previously stored response, or ``None`` when absent."""

        ...

    async def put(
        self,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: object,
        correlation_id: str,
    ) -> None:
        """Persist a successful response for future replay."""

        ...


__all__ = ["IdempotencyHttpDependencyProvider"]
