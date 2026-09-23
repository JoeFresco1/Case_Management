"""Stable audit event types, subject type, and adapter ref for connector use cases.

The ``EVENT_*`` strings are written to the audit trail and the outbox by
:mod:`.draft`, :mod:`.admin`, and :mod:`.retire`; the ``SUBJECT_TYPE_*``
string labels audit rows in :func:`.helpers._emit_audit`. Split out of the
former single-file ``manage_connectors.py`` during the R2 package split.

Why plain literals and not an Enum: the operator console and timeline
filters key on these exact strings — changing a wire value would orphan
historical audit rows.
"""

from __future__ import annotations

from typing import Final

# Stable event types used by the audit + outbox trail. Strings are
# the contract; subscribers and the operator console key on them.
EVENT_CONNECTOR_DRAFTED = "ConnectorDrafted"
EVENT_CONNECTOR_APPROVED = "ConnectorApproved"
EVENT_CONNECTOR_ENABLED = "ConnectorEnabled"
EVENT_CONNECTOR_DRAIN_STARTED = "ConnectorDrainStarted"
EVENT_CONNECTOR_DRAIN_CONFIRMED = "ConnectorDrainConfirmed"
EVENT_CONNECTOR_RETIRED = "ConnectorRetired"
EVENT_CONNECTOR_MONITORED = "ConnectorMonitored"

SUBJECT_TYPE_CONNECTOR: Final[str] = "connector"

# Default adapter reference for synthetic connectors. The first slice
# has a single local adapter; the production adapter is a separate
# seam (ADR-015, AGENTS.md §12).
DEFAULT_SYNTHETIC_ADAPTER_REF: Final[str] = "app.infrastructure.connectors.synthetic"

__all__ = [
    "DEFAULT_SYNTHETIC_ADAPTER_REF",
    "EVENT_CONNECTOR_APPROVED",
    "EVENT_CONNECTOR_DRAIN_CONFIRMED",
    "EVENT_CONNECTOR_DRAIN_STARTED",
    "EVENT_CONNECTOR_DRAFTED",
    "EVENT_CONNECTOR_ENABLED",
    "EVENT_CONNECTOR_MONITORED",
    "EVENT_CONNECTOR_RETIRED",
    "SUBJECT_TYPE_CONNECTOR",
]
