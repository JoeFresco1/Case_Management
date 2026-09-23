"""Protocols and frozen I/O types for the SIEM stream + signed batches (P09-US8, US9).

# INTEGRATION_SEAM: Azure SIEM — **Gated**

``SiemSink`` and ``SignedBatchRepository`` are the two application-layer
Ports the Gated Azure adapter (ADR-009) will implement later; the local
adapters live in ``app.infrastructure.persistence.siem``. The frozen
dataclasses are the use-case inputs/outputs and the wire shape of a
signed batch. Split out of the former single ``siem_stream`` module by
the R2 structural refactor; everything here is re-exported from
``app.application.audit.siem_stream`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .constants import DEFAULT_PAGE_LIMIT

__all__ = [
    "SignedBatch",
    "SignedBatchRepository",
    "SiemSink",
    "StreamQuery",
    "StreamingResult",
    "Any",
    "Protocol",
    "UUID",
    "dataclass",
    "datetime",
]


class SiemSink(Protocol):
    """A best-effort consumer of streamed audit rows (P09-US8).

    Local adapter: an in-process list (the test harness). Azure
    adapter: Event Hub / Service Bus forwarder (ADR-009 Gated). The
    sink MUST NOT raise on transport failure — the row is already
    durable in ``audit_events``; a sink error is recorded in
    telemetry and the stream continues with the next row.
    """

    async def send(
        self,
        *,
        tenant_id: UUID,
        row: dict[str, Any],
    ) -> None: ...


class SignedBatchRepository(Protocol):
    """Persistence port for signed audit batches (P09-US9 / FM-046).

    Local adapter: in-memory (test + v1). Azure adapter: a
    tamper-evident blob store behind the signature (Gated). Every
    method is tenant-scoped; cross-tenant reads return ``None`` so
    the use case surfaces 404 without leaking existence.
    """

    async def add(
        self,
        batch: SignedBatch,
    ) -> None: ...

    async def get(
        self,
        *,
        tenant_id: UUID,
        batch_id: UUID,
    ) -> SignedBatch | None: ...

    async def list(
        self,
        *,
        tenant_id: UUID,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> list[SignedBatch]: ...


@dataclass(frozen=True, slots=True)
class StreamQuery:
    """The input to :func:`app.application.audit.siem_stream.handle_stream`.

    ``since`` / ``until`` mirror the export query. ``tenant_id`` is
    always the actor's tenant; the use case refuses cross-tenant
    reads defensively even though the store is already scoped.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_LIMIT


@dataclass(frozen=True, slots=True)
class StreamingResult:
    """The stream use case return value.

    ``rows_streamed`` is the number of rows pushed to the sink.
    ``rows_skipped`` counts sink transport failures (the row is
    already durable; the skip is a best-effort telemetry signal,
    not a durable loss). ``first_event_id`` and ``last_event_id``
    span the durable range regardless of how many rows the sink
    actually accepted.
    """

    rows_streamed: int
    rows_skipped: int
    first_event_id: UUID | None = None
    last_event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SignedBatch:
    """A tamper-evident attestation over a contiguous audit span.

    The shape is what the auditor's tooling receives off the wire.
    The ``payload`` field is the canonical JSON of every row in the
    batch (NOT the raw rows — those are re-read from
    ``audit_events``); the ``signature`` is computed over
    ``canonical_json(payload)`` keyed with the per-tenant secret.
    """

    id: UUID
    tenant_id: UUID
    first_event_id: UUID
    last_event_id: UUID
    first_event_hash: str
    last_event_hash: str
    row_count: int
    algorithm: str
    key_id: str
    signature: str
    payload: dict[str, Any]
    created_at: datetime
    created_by: UUID
