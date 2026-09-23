"""Storage / messaging / identity / secrets / telemetry -- placeholder Protocols.

Implementations land in their respective phases; declared here so the
dependency direction is fixed from day one.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class DocumentStore(Protocol):
    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str: ...

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes: ...


class MessageBus(Protocol):
    async def publish(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None: ...


class SecretProvider(Protocol):
    """Async secret-resolution port (AGENTS.md §12 secrets seam).

    Implementations: local env + JSON file (current), Azure Key Vault
    (production seam). The port is intentionally narrow -- only what the
    application layer needs to resolve a secret by logical name. Writes
    are local-only; production adapters MUST be append-only from the
    application's perspective (set_secret is a convenience for local
    dev seeding and is a no-op or rejection in production adapters).
    """

    async def get(self, name: str) -> str:
        """Resolve a secret by logical name.

        Raises:
            KeyError: when the secret cannot be resolved.
        """

    async def set_secret(self, name: str, value: str) -> None:
        """Store a secret locally.

        Local adapter writes through to the JSON store; production
        adapters should raise NotImplementedError because secret
        rotation belongs in the operator workflow, not the app.
        """


class Telemetry(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...


__all__ = [
    "DocumentStore",
    "MessageBus",
    "SecretProvider",
    "Telemetry",
]
