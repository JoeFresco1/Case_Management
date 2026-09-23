"""Dependency contracts for participant HTTP composition."""

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, CaseRepository, OutboxWriter, ParticipantRepository


@dataclass(frozen=True, slots=True)
class ParticipantHttpRepositories:
    """Ports sharing one request transaction."""

    case: CaseRepository
    participant: ParticipantRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class ParticipantHttpDependencyProvider(Protocol):
    """Build participant ports for one persistence session."""

    def for_session(self, session: object) -> ParticipantHttpRepositories:
        """Return transaction-bound participant ports."""


__all__ = ["ParticipantHttpDependencyProvider", "ParticipantHttpRepositories"]
