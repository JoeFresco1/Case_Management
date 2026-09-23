"""Audit-append helper shared by the subscription and enqueue use cases."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.context import Clock
from app.domain.ports import AuditEventStore


async def _record_audit(
    audit: AuditEventStore,
    *,
    tenant_id: UUID,
    event_type: str,
    actor_id: UUID | None,
    subject_id: UUID,
    correlation_id: str | None,
    payload: dict[str, Any],
    clock: Clock,
) -> None:
    """Append one audit row with the canonical fields filled in.

    Why a wrapper: the :class:`AuditEventStore.append` Protocol takes
    eleven keyword arguments, and every call site here shares
    ``actor_type="system"`` (or ``"user"`` for actor-supplied calls),
    ``subject_type`` derived from the event_type, ``case_id=None``,
    ``causation_id=None``. Centralizing the wrapper keeps the
    call sites short and ensures the canonical fields stay
    consistent across the five audit writes in this module.
    """
    subject_type = (
        "WebhookSubscription"
        if event_type.startswith("WebhookSubscription")
        else "OutboundDelivery"
    )
    actor_type = "user" if actor_id is not None else "system"
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id if actor_id is not None else subject_id,
        actor_type=actor_type,
        subject_type=subject_type,
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id or "",
        causation_id=None,
        payload=payload,
        occurred_at=clock.now(),
    )


__all__ = [
    "AuditEventStore",
    "Clock",
    "_record_audit",
]
