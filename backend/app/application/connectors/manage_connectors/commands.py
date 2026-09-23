"""Command / result dataclasses for the connector lifecycle use cases (P14-US5, US7 / FM-058).

Frozen value objects passed from the HTTP layer into the use-case functions
in :mod:`.draft`, :mod:`.admin`, and :mod:`.retire`. Split out of the former
single-file ``manage_connectors.py`` during the R2 package split; no behavior
lives here — only typed inputs and outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.connector import Connector, ConnectorKind


@dataclass(frozen=True, slots=True)
class DraftConnectorCommand:
    """The input to drafting a new connector.

    ``authorization_rationale`` is required for named connectors
    (the domain refuses to construct the row without it). For the
    synthetic kind it is optional but recommended.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    key: str
    kind: ConnectorKind
    design_summary: str
    authorization_rationale: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ApproveConnectorCommand:
    """The input to recording an approval (DRAFT → APPROVED)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    connector_id: UUID
    rationale: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class EnableConnectorCommand:
    """The input to enabling an approved connector (APPROVED → ENABLED)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    connector_id: UUID
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class MonitorConnectorQuery:
    """The input to reading the connector's monitor snapshot."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    connector_id: UUID


@dataclass(frozen=True, slots=True)
class RetireConnectorCommand:
    """The input to retiring a connector (ENABLED → DRAINING → RETIRED).

    ``drain_notes`` are recorded on the row's ``drain_notes`` field and
    in the audit payload so the retirement trail is self-describing.
    ``in_flight`` / ``completed`` / ``rejected`` are the operator's
    confirmation snapshot; the use case refuses to retire if
    ``in_flight > 0`` (Constitution V — never silently lose work).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    connector_id: UUID
    drain_notes: str
    in_flight: int
    completed: int
    rejected: int
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DraftConnectorResult:
    """The freshly drafted connector in ``DRAFT`` state."""

    connector: Connector


@dataclass(frozen=True, slots=True)
class ApproveConnectorResult:
    """The connector after the (possibly idempotent) approval."""

    connector: Connector


@dataclass(frozen=True, slots=True)
class EnableConnectorResult:
    """The connector after the (possibly idempotent) enable."""

    connector: Connector


@dataclass(frozen=True, slots=True)
class MonitorConnectorResult:
    """A read-only snapshot of the connector's lifecycle and counters.

    ``in_flight`` / ``completed`` / ``rejected`` are the latest values
    the operator confirmed during drain. The HTTP layer surfaces
    these so the operator can see "how much work is the drainer
    chewing through" without a second query.
    """

    connector: Connector
    in_flight: int
    completed: int
    rejected: int
    accepts_traffic: bool
    is_terminal: bool


@dataclass(frozen=True, slots=True)
class RetireConnectorResult:
    """The result of a successful retire command.

    ``drained`` indicates whether the connector went
    ENABLED → DRAINING → RETIRED in a single call (the operator
    confirmed safe-drain inline). A ``drained=False`` result means the
    connector is in ``DRAINING`` and a follow-up retire is required
    once the safe-drain invariant is confirmed.
    """

    connector: Connector
    drained: bool


__all__ = [
    "ApproveConnectorCommand",
    "ApproveConnectorResult",
    "DraftConnectorCommand",
    "DraftConnectorResult",
    "EnableConnectorCommand",
    "EnableConnectorResult",
    "MonitorConnectorQuery",
    "MonitorConnectorResult",
    "RetireConnectorCommand",
    "RetireConnectorResult",
]
