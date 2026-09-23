"""In-memory adapters for the SIEM / signed-batch surface (P09-US8, US9 / FM-046).

# INTEGRATION_SEAM: Azure SIEM — **Gated**

These adapters satisfy the two Protocols declared in
``app.application.audit.siem_stream``:

- :class:`InMemorySiemSink` — collects streamed events into a list
  the test harness can assert on. The Azure adapter (Event Hub /
  Service Bus forwarder) lands with the production target selection
  (ADR-009 Gated).
- :class:`InMemorySignedBatchRepository` — stores
  :class:`app.application.audit.siem_stream.SignedBatch` instances
  in a per-tenant list. The Azure adapter lands with the durable
  signature blob store (ADR-009 Gated).

Both are wired in tests and may also be used for local dev. They
share the local-dev secrets policy: the synthetic-environment HMAC
key is the only secret in play, and the v1 signature algorithm
("HMAC-SHA-256") is the explicit version pin.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from app.application.audit.siem_stream import (
    DEFAULT_PAGE_LIMIT,
    SiemSink,
    SignedBatch,
    SignedBatchRepository,
)


class InMemorySiemSink(SiemSink):
    """Collects streamed events for assertion in tests.

    The Azure adapter is registered once the SIEM target is
    selected (ADR-009 Gated). Until then this adapter is the only
    available sink. The class is intentionally tiny: there is no
    threading, no batching, no retry — the test asserts on the
    per-tenant lists in the order events were sent.
    """

    def __init__(self) -> None:
        self._rows: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        self.errors: list[Exception] = []

    async def send(self, *, tenant_id: UUID, row: dict[str, Any]) -> None:
        # The Protocol is "best-effort" — a sink error does not raise
        # out of the stream. We still want a test to be able to
        # simulate a failure, so the test can pre-load ``self.errors``
        # with a single exception that is raised (and recorded) on
        # the next call. The default behavior is to capture the row.
        if self.errors:
            raise self.errors.pop(0)
        self._rows[tenant_id].append(row)

    def events_for(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Return the rows sent for ``tenant_id`` in send order."""
        return list(self._rows.get(tenant_id, ()))

    def clear(self) -> None:
        self._rows.clear()
        self.errors.clear()


class InMemorySignedBatchRepository(SignedBatchRepository):
    """Per-tenant in-memory storage for :class:`SignedBatch` instances.

    Tenant isolation is the first lookup key so a misconfigured
    caller cannot cross-reference batches across tenants. ``get``
    returns ``None`` for a missing or cross-tenant id so the use
    case surfaces 404 without leaking existence (Constitution V).
    """

    def __init__(self) -> None:
        self._batches: dict[UUID, dict[UUID, SignedBatch]] = defaultdict(dict)

    async def add(self, batch: SignedBatch) -> None:
        self._batches[batch.tenant_id][batch.id] = batch

    async def get(
        self,
        *,
        tenant_id: UUID,
        batch_id: UUID,
    ) -> SignedBatch | None:
        return self._batches.get(tenant_id, {}).get(batch_id)

    async def list(
        self,
        *,
        tenant_id: UUID,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> list[SignedBatch]:
        ordered = sorted(
            self._batches.get(tenant_id, {}).values(),
            key=lambda b: b.created_at,
        )
        return ordered[:limit]


__all__ = [
    "InMemorySiemSink",
    "InMemorySignedBatchRepository",
]
