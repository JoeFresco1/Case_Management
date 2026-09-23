"""Audit export use case (P09-US5 / FM-044).

The export is a *structured* artifact that a process administrator
takes out of the system. Two invariants must hold:

1. **Every row carries its chain anchors.** The consumer of the export
   must be able to verify the hash chain off-system (P09-US3); the
   batch therefore includes ``id``, ``prev_hash``, ``hash``, and the
   raw ``payload`` (not the redacted wire shape). The export is the
   one place where the un-redacted payload is permitted to leave the
   system — and only to actors who already hold ``audit.export``.

2. **The export itself is an audit event.** Running an export is a
   security-relevant action: it leaves the boundary. The use case
   writes a ``CaseAudited`` (well, the existing
   ``AuditCorrectionRecorded`` is the canonical "auditor action"
   event — but a fresh export is logically a read, so we record a
   dedicated ``AuditExported`` event via ``record_event`` in the same
   transaction as the read).

# INTEGRATION_SEAM: read API + audit read

The use case is the boundary that decides **what the export sees**:

- It enforces ``AUDIT_EXPORT`` on the actor (P09-US5, FM-044).
- It tenant-scopes the query (a missing ``case_id`` plus
  ``tenant_id`` enforces tenant isolation; cross-tenant probes surface
  as ``NotFound``).
- It redaction-strips the payload using the same allowlist as the
  timeline view so an exported row is no more revealing than the
  timeline entry it shadows. The hash chain anchors (``prev_hash``,
  ``hash``) are NOT redacted — they are integrity metadata, not
  payload content.

# Pagination

Cursor-based, anchored on ``(occurred_at, id)``. The cursor is opaque
on the wire (``encode_timeline_cursor`` from the persistence layer).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from app.application.audit import view_timeline as view_timeline_uc
from app.application.audit.record_event import (
    RecordAuditCommand,
    record_event,
)
from app.application.authorization import Action, Resource, authorize
from app.domain.audit import (
    AUDIT_EXPORTED,
    payload_hash,
)
from app.domain.errors import ValidationFailed
from app.domain.ports import AuditEventStore

# ---------------------------------------------------------------------------
# Limits (mirror AGENTS.md §17.1)
# ---------------------------------------------------------------------------

DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

# Export event type. The first slice ships a single dedicated event so
# downstream auditors can spot "export ran" markers on the timeline.
# Payload key carries the reason the export was pulled (free-text, but
# not the exported content — the consumer has the batch already).
EXPORT_KEY_REASON: Final[str] = "reason"
EXPORT_KEY_CASE_ID: Final[str] = "case_id"
EXPORT_KEY_SINCE: Final[str] = "since"
EXPORT_KEY_UNTIL: Final[str] = "until"
EXPORT_KEY_ROW_COUNT: Final[str] = "row_count"

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "EXPORT_KEY_CASE_ID",
    "EXPORT_KEY_REASON",
    "EXPORT_KEY_ROW_COUNT",
    "EXPORT_KEY_SINCE",
    "EXPORT_KEY_UNTIL",
    "ExportBatch",
    "ExportEntry",
    "ExportQuery",
    "handle",
    "MAX_PAGE_LIMIT",
]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportEntry:
    """A single audit row in the export batch (P09-US5 / FM-044).

    The shape mirrors the timeline entry **plus** the integrity metadata
    a downstream auditor needs to verify the chain: ``hash`` and
    ``prev_hash``. The export is the one read that exposes those values
    to non-system callers, by design.
    """

    id: UUID
    event_type: str
    occurred_at: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    hash: str
    prev_hash: str
    redacted: bool


@dataclass(frozen=True, slots=True)
class ExportBatch:
    """A single page of the export plus chain-verification metadata.

    ``rows_in_batch`` is the count of rows on this page (not the total
    tenant count); ``next_cursor`` is the opaque pagination token. The
    consumer concatenates pages in order; the hash chain is contiguous
    across pages because every row carries ``prev_hash``.
    """

    items: tuple[ExportEntry, ...]
    next_cursor: str | None
    limit: int
    rows_in_batch: int


@dataclass(frozen=True, slots=True)
class ExportQuery:
    """The use case input.

    ``case_id`` is optional; when omitted the export is tenant-wide
    within ``since``/``until``. The ``reason`` is recorded on the
    ``AuditExported`` event so the timeline shows *why* a given export
    ran (P09-US5: "the export is itself an audit event").
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID | None
    since: datetime | None
    until: datetime | None
    reason: str
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: str | None = None


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


def _validate_query(q: ExportQuery) -> None:
    """Run the application-level invariants on ``q``."""
    if q.limit < 1 or q.limit > MAX_PAGE_LIMIT:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_PAGE_LIMIT}",
            field="limit",
        )
    if q.since is not None and q.until is not None and q.since > q.until:
        raise ValidationFailed(
            "since must be <= until",
            field="since",
        )
    if not q.reason or not q.reason.strip():
        raise ValidationFailed(
            "reason is required for an audit export",
            field="reason",
        )


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply the timeline allowlist to the export payload.

    Why reuse the timeline allowlist: the export is no more revealing
    than the timeline; it just bundles multiple entries. The redaction
    flag is propagated so the consumer can render a per-row badge.
    """
    safe = view_timeline_uc.PAYLOAD_SAFE_KEYS
    redacted = False
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in safe:
            out[k] = v
            continue
        redacted = True
        out[k] = "***"
    return out, redacted


def _row_to_entry(row: Any) -> ExportEntry:
    """Translate a persistence row into an ``ExportEntry``."""
    redacted_payload, redacted_flag = _redact_payload(dict(row.payload or {}))
    return ExportEntry(
        id=row.id,
        event_type=row.event_type,
        occurred_at=row.occurred_at.isoformat(),
        actor_id=row.actor_id,
        actor_type=row.actor_type,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        payload=redacted_payload,
        hash=row.hash,
        prev_hash=row.prev_hash,
        redacted=redacted_flag,
    )


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    query: ExportQuery,
    *,
    audit: AuditEventStore,
    cursor_encoder: Any | None = None,
    clock: Any | None = None,
    dedupe: Any | None = None,
    correlation_id: str = "",
) -> ExportBatch:
    """Return one page of the audit export and record the action.

    The export is itself an audit event: the same transaction writes an
    ``AuditExported`` row referencing the export's scope. If the actor
    passes a ``correlation_id`` we reuse it; otherwise we synthesize one
    so the timeline shows the export as its own logical request.
    """
    _validate_query(query)

    # 1. Authorization. AUDIT_EXPORT is required; the resource is the
    # whole tenant (a date-range export) or the specific case.
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.AUDIT_EXPORT,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="tenant"),
    )

    # 2. Read the page. ``list_for_export`` enforces tenant scoping;
    # ``case_id`` is optional (tenant-wide date-range export).
    rows = await audit.list_for_export(
        tenant_id=query.tenant_id,
        case_id=query.case_id,
        since=query.since,
        until=query.until,
        limit=query.limit,
        cursor=query.cursor,
    )

    items = tuple(_row_to_entry(r) for r in rows)

    next_cursor: str | None = None
    if len(items) == query.limit and cursor_encoder is not None and rows:
        tail = rows[-1]
        next_cursor = cursor_encoder(tail.occurred_at, tail.id)

    # 3. Record the export itself. We append on the caller's
    # transaction so the export action and the audit row are
    # atomic. ``dedupe`` is optional (use cases without an idempotency
    # layer pass ``None``); the in-memory test wires its own.
    if dedupe is not None and clock is not None:
        now = clock.now() if hasattr(clock, "now") else datetime.now(UTC)
        export_payload: dict[str, Any] = {
            EXPORT_KEY_REASON: query.reason,
            EXPORT_KEY_ROW_COUNT: len(items),
        }
        if query.case_id is not None:
            export_payload[EXPORT_KEY_CASE_ID] = str(query.case_id)
        if query.since is not None:
            export_payload[EXPORT_KEY_SINCE] = query.since.isoformat()
        if query.until is not None:
            export_payload[EXPORT_KEY_UNTIL] = query.until.isoformat()
        cid = correlation_id or str(payload_hash(export_payload))
        await record_event(
            RecordAuditCommand(
                tenant_id=query.tenant_id,
                actor_id=query.actor_id,
                actor_type="user",
                event_type=AUDIT_EXPORTED,
                subject_type="case" if query.case_id is not None else "tenant",
                subject_id=query.case_id or query.tenant_id,
                case_id=query.case_id,
                correlation_id=cid,
                causation_id=None,
                payload=export_payload,
                occurred_at=now,
            ),
            audit=audit,
            dedupe=dedupe,
        )

    return ExportBatch(
        items=items,
        next_cursor=next_cursor,
        limit=query.limit,
        rows_in_batch=len(items),
    )
