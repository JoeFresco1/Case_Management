"""SLA scanning use cases (P02-US6 / FM-018)."""

from app.application.sla.scan_sla import (
    DEFAULT_SCAN_BATCH,
    OUTBOX_EVENT_SLA_BREACHED,
    ScanSlaResult,
    scan_once,
)

__all__ = [
    "DEFAULT_SCAN_BATCH",
    "OUTBOX_EVENT_SLA_BREACHED",
    "ScanSlaResult",
    "scan_once",
]
