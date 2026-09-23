"""Channel + quarantine ports (P04-US4, US5 / FM-027)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.channel import Channel, QuarantineRecord, QuarantineStatus
from app.domain.ports.core import TenantScoped


class ChannelRepository(TenantScoped, Protocol):
    """Persistence port for :class:`Channel` aggregates.

    Every method is tenant-scoped: cross-tenant reads return ``None``
    so a misconfigured caller cannot leak channel existence
    (Constitution V).
    """

    async def get(self, tenant_id: UUID, channel_id: UUID) -> Channel | None: ...

    async def get_by_key(self, tenant_id: UUID, key: str) -> Channel | None: ...

    async def list(self, tenant_id: UUID, *, enabled_only: bool = False) -> Sequence[Channel]: ...

    async def add(self, channel: Channel) -> None: ...

    async def update(self, channel: Channel) -> None: ...


class QuarantineRepository(TenantScoped, Protocol):
    """Persistence port for :class:`QuarantineRecord` aggregates.

    The repository is the integration point for the operator console:
    the application layer's ``inspect`` / ``release`` / ``reject`` use
    cases resolve the record via this port. Records are append-only in
    status terms -- once ``RELEASED`` or ``REJECTED``, the record's
    payload snapshot cannot be re-fetched (the resolution fields are
    the only mutating surface).
    """

    async def get(self, tenant_id: UUID, quarantine_id: UUID) -> QuarantineRecord | None: ...

    async def list(
        self,
        tenant_id: UUID,
        *,
        channel_id: UUID | None = None,
        status: QuarantineStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[QuarantineRecord]: ...

    async def add(self, record: QuarantineRecord) -> None: ...

    async def update(self, record: QuarantineRecord) -> None: ...


__all__ = [
    "ChannelRepository",
    "QuarantineRepository",
]
