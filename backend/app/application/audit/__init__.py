"""Audit use cases (P09 / FM-003).

This package is the application-layer boundary for audit writes. The
domain owns the event-type catalog and the hash chain (in
``app.domain.audit``); the infrastructure owns the SQL adapter (in
``app.infrastructure.persistence.audit_outbox``); the use case in
``record_event`` owns the idempotency and the validation contract.
"""

from __future__ import annotations

__all__: list[str] = []
