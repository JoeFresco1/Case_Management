"""Application use cases for the audit store (P09-US2, US3, US7; FM-003).

The audit store is append-only at four layers (AGENTS.md §4.R12,
plan §4.R12). This module is the *application* layer; it owns the
business rule that "two writes with the same (event_type,
correlation_id, payload_hash) must collapse to one row."

# Why an application-layer idempotency rule

The HTTP layer caches the response by ``Idempotency-Key`` (AGENTS.md
§10, plan §4.R7) but use cases can also be invoked from background
jobs, the outbox dispatcher, and CLI scripts. Without an
application-level guard, a retry inside a retry would write two audit
rows with the same correlation id and the same payload — which the
hash chain would still accept (the second row's hash is computed from
the first's), but which would inflate the timeline and break the
"exactly one audit row per domain change" contract from P09-US2.

The idempotency key is ``(tenant_id, event_type, correlation_id,
payload_hash)``. ``payload_hash`` is the canonical SHA-256 over the
payload (NOT including ``tenant_id`` / ``correlation_id``) so a retry
that mutated any payload field is treated as a *different* write and
is rejected as a conflict (rather than silently dropped).

# Same-transaction guarantee

``record`` does **not** commit. It queues the row on the same
``AsyncSession`` the caller is using for the domain change. The caller
commits once; on rollback the audit row is rolled back too. This is
the P09-US2 / I2 acceptance scenario.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.domain.audit import (
    ALL_EVENT_TYPES,
    AUDIT_CORRECTION_RECORDED,
    CORRECTION_KEY_ORIGINAL_EVENT_ID,
    AuditEvent,
    make_correction_payload,
    payload_hash,
)
from app.domain.errors import ValidationFailed
from app.domain.ports import AuditEventStore

# A ``Dedupe`` callable that returns True iff a prior row with the same
# idempotency key already exists in this session/transaction. The
# application layer is implementation-agnostic; the production
# adapter (and the in-memory test fake) supply the lookup.
Dedupe = Callable[[UUID, str, str, str], bool]


# Sentinel returned by ``record_event`` on a silent idempotent replay.
# Callers that need to distinguish "appended" from "deduped" compare
# against ``DEDUPED_RETURNED_EVENT``. The UUID is the all-zeros sentinel;
# the event_type is the literal string ``__deduped__`` so accidental
# collisions with real rows are detectable in logs.
DEDUPED_RETURNED_EVENT: Final[AuditEvent] = AuditEvent(
    tenant_id=UUID(int=0),
    event_type="__deduped__",
    actor_id=UUID(int=0),
    actor_type="system",
    subject_type="__deduped__",
    subject_id=UUID(int=0),
    case_id=None,
    correlation_id="__deduped__",
    causation_id=None,
    payload={"deduped": True},
)


@dataclass(frozen=True, slots=True)
class RecordAuditCommand:
    """The input to ``record_event.handle``.

    Mirrors the audit store's ``append`` shape plus a few use-case-level
    invariants. Keeping the command a frozen dataclass means the
    ``payload`` is a structural copy, not a shared reference — the
    caller cannot mutate it after the row is queued.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_type: str
    event_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


def _validate_command(cmd: RecordAuditCommand) -> None:
    """Run the application-level invariants on ``cmd``.

    Why here: the use case is the boundary between adapters (which
    speak in UUID strings and timestamps) and the domain (which trusts
    only valid values). A missing event type or a blank correlation id
    is rejected up front so the store never sees malformed rows.
    """
    if cmd.event_type not in ALL_EVENT_TYPES:
        # Why raise ``ValidationFailed``: AGENTS.md §10 / plan §4.R7
        # maps validation failures to a structured 422 via
        # ``interfaces/http/errors.py``.
        raise ValidationFailed(
            f"unknown audit event type: {cmd.event_type!r}",
            field="event_type",
        )
    if not cmd.correlation_id:
        raise ValidationFailed(
            "correlation_id is required for every audit row",
            field="correlation_id",
        )
    if cmd.actor_type not in {"user", "system", "service"}:
        raise ValidationFailed(
            f"unknown actor_type: {cmd.actor_type!r}",
            field="actor_type",
        )
    if cmd.event_type == AUDIT_CORRECTION_RECORDED and (
        CORRECTION_KEY_ORIGINAL_EVENT_ID not in cmd.payload
    ):
        # Correction payloads have a reserved shape (P09-US7).
        raise ValidationFailed(
            "AuditCorrectionRecorded requires original_event_id",
            field="payload",
        )


async def record_event(
    cmd: RecordAuditCommand,
    *,
    audit: AuditEventStore,
    dedupe: Dedupe,
) -> AuditEvent:
    """Append an audit row in the caller's transaction.

    Returns the staged ``AuditEvent`` so the caller can attach it to
    the timeline response or pass it downstream. Idempotent on
    ``(tenant_id, event_type, correlation_id, payload_hash)``.

    Returns ``DEDUPED_RETURNED_EVENT`` (a sentinel) on a silent
    idempotent replay so the caller can decide whether to surface
    that fact in a response or log line.

    Raises:
        ValidationFailed: when ``cmd`` carries invalid event_type,
            actor_type, correlation_id, or correction payload shape.
    """
    _validate_command(cmd)
    phash = payload_hash(cmd.payload)
    if dedupe(cmd.tenant_id, cmd.event_type, cmd.correlation_id, phash):
        # Same key + same payload hash -> silent no-op (idempotent
        # replay; AGENTS.md §10). We do NOT raise; the caller's
        # domain change already committed once and we are guarding
        # against a double-write inside the same logical request.
        return DEDUPED_RETURNED_EVENT
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=cmd.event_type,
        actor_id=cmd.actor_id,
        actor_type=cmd.actor_type,
        subject_type=cmd.subject_type,
        subject_id=cmd.subject_id,
        case_id=cmd.case_id,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        payload=cmd.payload,
        occurred_at=cmd.occurred_at,
    )
    return AuditEvent(
        tenant_id=cmd.tenant_id,
        event_type=cmd.event_type,
        actor_id=cmd.actor_id,
        actor_type=cmd.actor_type,
        subject_type=cmd.subject_type,
        subject_id=cmd.subject_id,
        case_id=cmd.case_id,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        payload=cmd.payload,
    )


@dataclass(frozen=True, slots=True)
class RecordCorrectionCommand:
    """A typed command for ``record_correction``.

    Why a separate command: a correction is a public, security-relevant
    action (P09-US7, FM-044). Forcing the caller to pass
    ``original_event_id``, ``reason``, and ``corrected_summary`` as
    explicit fields makes the API surface checkable from the type
    system and prevents accidental payload drift.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    original_event_id: str
    reason: str
    corrected_summary: str
    occurred_at: datetime


async def record_correction(
    cmd: RecordCorrectionCommand,
    *,
    audit: AuditEventStore,
    dedupe: Dedupe,
) -> AuditEvent:
    """Append a compensating ``AuditCorrectionRecorded`` row.

    The original event is never mutated (Constitution IV, P09-US7,
    plan §4.R12). The correction is a *new* row whose payload
    references the original by id.

    The correction uses the caller's correlation id so it lands in the
    same per-request timeline slice.
    """
    payload = make_correction_payload(
        original_event_id=cmd.original_event_id,
        reason=cmd.reason,
        corrected_summary=cmd.corrected_summary,
    )
    return await record_event(
        RecordAuditCommand(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_type=cmd.actor_type,
            event_type=AUDIT_CORRECTION_RECORDED,
            subject_type=cmd.subject_type,
            subject_id=cmd.subject_id,
            case_id=cmd.case_id,
            correlation_id=cmd.correlation_id,
            causation_id=cmd.causation_id,
            payload=payload,
            occurred_at=cmd.occurred_at,
        ),
        audit=audit,
        dedupe=dedupe,
    )


__all__ = [
    "DEDUPED_RETURNED_EVENT",
    "Dedupe",
    "RecordAuditCommand",
    "RecordCorrectionCommand",
    "record_correction",
    "record_event",
]
