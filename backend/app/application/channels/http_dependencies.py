"""Dependency contracts for the channels/quarantine HTTP composition boundary.

The channels router needs several ports bound to one request transaction.
This module describes that bundle without importing FastAPI, SQLAlchemy, or
any infrastructure adapter. The composition root supplies the concrete
provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    ChannelRepository,
    OutboxWriter,
    QuarantineRepository,
    RoleRepository,
)


@dataclass(frozen=True, slots=True)
class ChannelHttpRepositories:
    """Ports required by the channels/quarantine HTTP endpoints for one session."""

    channel: ChannelRepository
    quarantine: QuarantineRepository
    role: RoleRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class ChannelHttpDependencyProvider(Protocol):
    """Create a request-scoped channel port bundle from a persistence session."""

    def for_session(self, session: object) -> ChannelHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["ChannelHttpDependencyProvider", "ChannelHttpRepositories"]
