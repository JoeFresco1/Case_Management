"""Connector lifecycle commands, contracts, and application operations.

This package replaces the former ``manage_connectors.py`` module. The
public names remain available from the same import path.
"""

from __future__ import annotations

from .commands import (
    ApproveConnectorCommand,
    ApproveConnectorResult,
    DraftConnectorCommand,
    DraftConnectorResult,
    EnableConnectorCommand,
    EnableConnectorResult,
    MonitorConnectorQuery,
    MonitorConnectorResult,
    RetireConnectorCommand,
    RetireConnectorResult,
)
from .constants import (
    DEFAULT_SYNTHETIC_ADAPTER_REF,
    EVENT_CONNECTOR_APPROVED,
    EVENT_CONNECTOR_DRAFTED,
    EVENT_CONNECTOR_DRAIN_CONFIRMED,
    EVENT_CONNECTOR_DRAIN_STARTED,
    EVENT_CONNECTOR_ENABLED,
    EVENT_CONNECTOR_MONITORED,
    EVENT_CONNECTOR_RETIRED,
    SUBJECT_TYPE_CONNECTOR,
)
from .lifecycle import (
    approve_connector,
    draft_connector,
    enable_connector,
    monitor_connector,
    retire_connector,
)

__all__ = [
    "ApproveConnectorCommand",
    "ApproveConnectorResult",
    "DEFAULT_SYNTHETIC_ADAPTER_REF",
    "DraftConnectorCommand",
    "DraftConnectorResult",
    "EnableConnectorCommand",
    "EnableConnectorResult",
    "EVENT_CONNECTOR_APPROVED",
    "EVENT_CONNECTOR_DRAIN_CONFIRMED",
    "EVENT_CONNECTOR_DRAIN_STARTED",
    "EVENT_CONNECTOR_DRAFTED",
    "EVENT_CONNECTOR_ENABLED",
    "EVENT_CONNECTOR_MONITORED",
    "EVENT_CONNECTOR_RETIRED",
    "MonitorConnectorQuery",
    "MonitorConnectorResult",
    "RetireConnectorCommand",
    "RetireConnectorResult",
    "SUBJECT_TYPE_CONNECTOR",
    "approve_connector",
    "draft_connector",
    "enable_connector",
    "monitor_connector",
    "retire_connector",
]
