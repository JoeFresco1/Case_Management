"""Command / result dataclasses for the channel + quarantine use cases (P04-US4, US5 / FM-027).

Frozen value objects passed from the HTTP layer into the use-case functions
in :mod:`.admin` and :mod:`.quarantine`. Split out of the former single-file
``manage_channels.py`` during the R2 package split; no behavior lives here —
only typed inputs and outputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.domain.channel import (
    Channel,
    ChannelKind,
    PolicyDecision,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
)


@dataclass(frozen=True, slots=True)
class EnableChannelCommand:
    """Enable or configure a channel for the actor's tenant.

    When ``existing_channel_id`` is supplied the use case updates the
    policy / enabled flag on the existing row (idempotent reconfigure);
    otherwise it creates a fresh channel.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    key: str
    kind: ChannelKind
    policy: ValidationPolicy
    enabled: bool = True
    existing_channel_id: UUID | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DisableChannelCommand:
    """Disable a channel (fail-closed for new captures)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class QuarantineCaptureCommand:
    """A single captured item routed through a channel's policy.

    The application layer never reads ``values``' values into audit
    payloads — only the keys (via ``PolicyDecision.context``). The
    caller is responsible for the byte-level storage of the row (P04
    §6 D4 reserves payload redaction at the read side; this use case
    only persists the diagnosis).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID
    source_label: str
    values: Mapping[str, str]
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class InspectQuarantineQuery:
    """Read shape for the operator console."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID | None = None
    status: QuarantineStatus | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class ReleaseQuarantineCommand:
    """Release a held record back into the pipeline."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    quarantine_id: UUID
    note: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RejectQuarantineCommand:
    """Reject a held record permanently."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    quarantine_id: UUID
    note: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class EnableChannelResult:
    """The created or reconfigured channel, plus whether it is new."""

    channel: Channel
    created: bool


@dataclass(frozen=True, slots=True)
class DisableChannelResult:
    """The channel after the fail-closed disable."""

    channel: Channel


@dataclass(frozen=True, slots=True)
class QuarantineCaptureResult:
    """The result of routing one captured item through the policy.

    ``decision`` carries the policy verdict; ``record`` is non-None
    only when ``decision`` quarantines the row.
    """

    decision: PolicyDecision
    record: QuarantineRecord | None
    channel: Channel


@dataclass(frozen=True, slots=True)
class InspectQuarantineResult:
    """The held rows for one page of the operator console."""

    items: tuple[QuarantineRecord, ...]
    total: int


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """The record after a (possibly idempotent) release."""

    record: QuarantineRecord


@dataclass(frozen=True, slots=True)
class RejectResult:
    """The record after a (possibly idempotent) rejection."""

    record: QuarantineRecord


__all__ = [
    "DisableChannelCommand",
    "DisableChannelResult",
    "EnableChannelCommand",
    "EnableChannelResult",
    "InspectQuarantineQuery",
    "InspectQuarantineResult",
    "QuarantineCaptureCommand",
    "QuarantineCaptureResult",
    "RejectQuarantineCommand",
    "RejectResult",
    "ReleaseQuarantineCommand",
    "ReleaseResult",
]
