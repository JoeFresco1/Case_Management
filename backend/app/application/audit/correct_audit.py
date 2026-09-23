"""Append a compensating ``AuditCorrectionRecorded`` row (P09-US7 / FM-044).

A correction is **never** an UPDATE on the original event. It is a new
audit row whose payload references the original by id and carries:

- ``original_event_id`` (UUID of the row being corrected),
- ``reason`` (free-text, why the row is being annotated),
- ``corrected_summary`` (the new summary a downstream reader should
  see alongside the original).

The append is itself an audit event — the chain grows by exactly one
row per correction. The original row's payload, hash, and chain
position are untouched (Constitution IV, AGENTS.md §4.R12, P09-US7).

# INTEGRATION_SEAM: write API

The use case is the boundary that decides whether a correction is
allowed. It enforces:

- ``AUDIT_CORRECT`` on the actor (P09-US7 / FM-044).
- Tenant scoping (cross-tenant ``original_event_id`` surfaces as
  ``NotFound`` so an actor cannot probe for ids outside their
  tenant).
- Original-event integrity: the original must exist, must belong to
  the actor's tenant, and must NOT itself be a correction (a chain
  of corrections chained on corrections would obscure the original;
  the operator must edit the underlying source-of-truth, then
  append a single correction).

# Failure modes (mirrors AGENTS.md §4.R5)

- Missing original_event_id -> ``ValidationFailed`` (422).
- Original row not found in tenant -> ``NotFound`` (404).
- Original row is itself a correction -> ``Conflict`` (409).
- Actor missing ``AUDIT_CORRECT`` -> ``Forbidden`` (403).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.audit.record_event import (
    RecordCorrectionCommand,
    record_correction,
)
from app.application.authorization import Action, Resource, authorize
from app.domain.audit import AUDIT_CORRECTION_RECORDED, CORRECTION_KEY_REASON
from app.domain.errors import Conflict, NotFound, ValidationFailed
from app.domain.ports import AuditEventStore

__all__ = [
    "CorrectAuditCommand",
    "CorrectionResult",
    "handle",
]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorrectAuditCommand:
    """The use case input (P09-US7 / FM-044).

    The operator must pass every field explicitly; the use case does
    not silently default a ``reason`` or ``corrected_summary``.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    original_event_id: UUID
    reason: str
    corrected_summary: str
    correlation_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    """The use case return value.

    ``original_event_id`` echoes the id from the command. The
    ``AuditCorrectionRecorded`` event id is stamped by the store on
    commit; the use case does not know it. The router performs a
    re-read by correlation_id (same transaction) and surfaces the id
    via ``new_event_id``. We leave it as ``None`` here so the
    application layer never invents a row id.
    """

    original_event_id: UUID
    new_event_id: UUID | None
    correction_payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _validate_command(cmd: CorrectAuditCommand) -> None:
    """Run the application-level invariants on ``cmd``."""
    if not cmd.reason or not cmd.reason.strip():
        raise ValidationFailed(
            "reason is required for a correction",
            field="reason",
        )
    if not cmd.corrected_summary or not cmd.corrected_summary.strip():
        raise ValidationFailed(
            "corrected_summary is required for a correction",
            field="corrected_summary",
        )
    if not cmd.correlation_id:
        raise ValidationFailed(
            "correlation_id is required for every audit row",
            field="correlation_id",
        )


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    cmd: CorrectAuditCommand,
    *,
    audit: AuditEventStore,
    dedupe: Any,
    clock: Any | None = None,
) -> CorrectionResult:
    """Append a compensating correction row referencing ``original_event_id``.

    The original row is never modified (Constitution IV). The append
    goes through ``record_correction`` so it inherits the
    ``AuditCorrectionRecorded`` validation (must carry
    ``original_event_id``) and the application-layer idempotency on
    ``(tenant_id, event_type, correlation_id, payload_hash)``.

    Failure behavior:
    - Missing ``reason`` or ``corrected_summary`` -> ``ValidationFailed``.
    - Actor missing ``AUDIT_CORRECT`` -> ``Forbidden``.
    - Original row missing in tenant -> ``NotFound``.
    - Original row is itself an ``AuditCorrectionRecorded`` -> ``Conflict``
      (operators must annotate the underlying event, not the annotation).
    """
    _validate_command(cmd)

    # 1. Authorization. AUDIT_CORRECT is required; the resource is the
    # whole tenant (corrections are an administrative action, not
    # scoped to a specific subject).
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.AUDIT_CORRECT,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="tenant"),
    )

    # 2. Fetch the original. ``get_event`` is tenant-scoped; a row in
    # another tenant returns ``None`` and we surface 404 so the actor
    # cannot probe for cross-tenant ids (Constitution V).
    original = await audit.get_event(
        tenant_id=cmd.tenant_id,
        event_id=cmd.original_event_id,
    )
    if original is None:
        raise NotFound(f"audit event {cmd.original_event_id} not found in tenant")

    # 3. Refuse to annotate a correction. A correction is itself an
    # audit row carrying ``original_event_id`` in its payload; chaining
    # annotations hides the underlying source of truth.
    if original.event_type == AUDIT_CORRECTION_RECORDED:
        raise Conflict(
            "cannot annotate an AuditCorrectionRecorded; annotate the original event instead"
        )

    # 4. Append the correction. We thread the original's ``subject_*``
    # and ``case_id`` so the correction lands in the same per-request
    # timeline slice as the event it annotates (auditors expect the
    # pair to live together).
    now = cmd.occurred_at
    if clock is not None and hasattr(clock, "now"):
        now = clock.now()
    new_event = await record_correction(
        RecordCorrectionCommand(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type=original.subject_type,
            subject_id=original.subject_id,
            case_id=original.case_id,
            correlation_id=cmd.correlation_id,
            causation_id=str(cmd.original_event_id),
            original_event_id=str(cmd.original_event_id),
            reason=cmd.reason,
            corrected_summary=cmd.corrected_summary,
            occurred_at=now,
        ),
        audit=audit,
        dedupe=dedupe,
    )

    # ``record_correction`` returns the staged ``AuditEvent`` (or
    # ``DEDUPED_RETURNED_EVENT``). Pull the payload back out so the
    # router can echo it; we DO NOT mutate it.
    if hasattr(new_event, "payload") and isinstance(new_event.payload, dict):
        payload = dict(new_event.payload)
    else:  # pragma: no cover — defensive against the dedupe sentinel
        payload = {CORRECTION_KEY_REASON: cmd.reason}

    return CorrectionResult(
        original_event_id=cmd.original_event_id,
        new_event_id=None,
        correction_payload=payload,
    )
