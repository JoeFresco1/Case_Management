"""View the per-case audit timeline (P09-US1 / US4; FM-043 / FR-022).

The timeline is the canonical read view for the audit chain on a single
case: events in chronological order, each carrying its actor, event
type, timestamp, payload hash, and the case-type or workflow version
that was in effect when the event was recorded (AGENTS.md §16.3).

# INTEGRATION_SEAM: read API

The use case is the boundary that decides **what the wire sees**:

- It enforces ``AUDIT_READ`` on the actor; unauthorized callers are
  refused with ``Forbidden`` (Constitution V, AGENTS.md §4.R12).
- It tenant-scopes the query (every repository call carries the
  actor's ``tenant_id``; cross-tenant ids surface as ``NotFound``).
- It redaction-strips the payload: audit payloads reference protected
  values by id, never by content (P09 / FM-003, AGENTS.md §18). The
  view keeps the payload's structural keys but replaces any value
  that does not match the safe-shape allowlist with ``"***"`` so a
  leak cannot be read by a side channel.
- It exposes cursor-based pagination via the ``(occurred_at, id)`` tuple
  the SQL adapter orders by (AGENTS.md §17.1).

# Filter shape

The query carries optional ``event_types`` (multi-select),
``actor_id``, ``since``, ``until``. Empty / unspecified filters are
no-ops; the SQL adapter AND-s only the present ones. Validation of the
filter values lives here so the HTTP layer can render structured 422s
on malformed input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import ALL_EVENT_TYPES
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import AuditEventStore, CaseRepository

# ---------------------------------------------------------------------------
# Limits (AGENTS.md §17.1): pagination defaults; mirror the OpenAPI schema.
# ---------------------------------------------------------------------------

DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

# Timeline payloads may carry these top-level keys verbatim. Anything
# else is treated as protected content and redacted to ``"***"``. The
# allowlist is intentionally small: per AGENTS.md §16.2 the audit
# payload references protected values by id, never by content, so most
# event types only need to carry ``case_reference``, ``case_type_version_id``,
# ``workflow_version_id``, ``transition_key``, ``actor_role``, etc.
PAYLOAD_SAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "case_reference",
        "case_type_version_id",
        "case_type_key",
        "workflow_version_id",
        "workflow_state",
        "transition_key",
        "task_type",
        "task_id",
        "document_id",
        "version_number",
        "decision_type",
        "decision_id",
        "reason",
        "summary",
        "actor_role",
        "result",
        "original_event_id",
        "corrected_summary",
        # FM-017 SoD (T4-US2): the WorkflowTransitioned row stamps the
        # approval outcome so the timeline renders who approved what
        # without re-reading the workflow definition. All values are
        # ids/keys/reasons — never protected content.
        "action",
        "from_state",
        "to_state",
        "human_driven",
        "approval_required",
        "sod_outcome",
        "sod_reason",
        "originator_id",
        "approver_id",
        "task_count",
        "fields_written",
        "sla_started",
        "transitioned_at",
        # FM-018 SLA (T4-US3): the SlaBreached/SlaWarningRaised rows
        # carry the clock identity, deadline, and fired-action outcome
        # so the breach is visible in the case timeline. All values
        # are keys/timestamps/booleans — never protected content.
        "clock_key",
        "started_at",
        "due_at",
        "warning_at",
        "breached_at",
        "warning_at_now",
        "case_under_legal_hold",
        "escalation_fired",
        "escalation_type",
        "escalation_target",
    }
)


# ---------------------------------------------------------------------------
# Use case I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """A single audit row on the timeline wire shape (P09-US1, US4).

    ``payload`` is the redacted payload — sensitive values are replaced
    with ``"***"`` so the client cannot read protected content even when
    authorized to view the row.

    ``config_version`` is the configuration version that was in effect
    when the event was recorded (case-type or workflow version id,
    depending on the event type). The lookup is structural: if the
    payload carried a ``case_type_version_id`` key, that is the value;
    otherwise a ``workflow_version_id``; otherwise ``None``. The UI
    surfaces this so operators can explain "why this case behaves
    this way" against the version that was active at write time.
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
    config_version: str | None
    redacted: bool


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """The use case return value: a page of entries + a next cursor.

    ``next_cursor`` is the opaque wire token the SQL adapter emits; the
    router passes it straight back to the client. ``None`` means the
    page is the last page.
    """

    items: list[TimelineEntry]
    next_cursor: str | None
    limit: int


@dataclass(frozen=True, slots=True)
class ViewTimelineQuery:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    event_types: tuple[str, ...] | None = None
    actor_filter: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see view_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _validate_query(q: ViewTimelineQuery) -> None:
    """Run the application-level invariants on ``q``.

    Why here: keeps the use case the single validation boundary so the
    router can hand the query straight to FastAPI without re-validating.
    """
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
    if q.event_types:
        unknown = [t for t in q.event_types if t not in ALL_EVENT_TYPES]
        if unknown:
            raise ValidationFailed(
                f"unknown event types: {unknown!r}",
                field="event_types",
            )


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Replace values for unsafe keys with ``"***"``.

    Why a structural allowlist rather than per-field scrubbing: the
    audit payload schema is intentionally narrow (AGENTS.md §16.3 — the
    payload references protected values by id, never by content), so a
    fixed allowlist catches both the "added a sensitive key by
    accident" bug and the "left a free-text note in the payload" bug
    with one rule. ``redacted`` is True iff at least one value was
    replaced.
    """
    redacted = False
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in PAYLOAD_SAFE_KEYS:
            out[k] = v
            continue
        redacted = True
        out[k] = "***"
    return out, redacted


def _config_version(payload: dict[str, Any]) -> str | None:
    """Pick the configuration version recorded in ``payload``.

    Order: case-type first, workflow second. ``None`` when neither is
    present (e.g., ``AuditCorrectionRecorded`` events).
    """
    for key in ("case_type_version_id", "workflow_version_id"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _row_to_entry(row: Any) -> TimelineEntry:
    """Translate a persistence row into the wire-shaped ``TimelineEntry``.

    Why centralized: keeps the redaction + config-version rules in one
    place so a future "timeline column" addition doesn't drift between
    routers and tests.
    """
    redacted_payload, redacted = _redact_payload(dict(row.payload or {}))
    return TimelineEntry(
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
        config_version=_config_version(redacted_payload),
        redacted=redacted,
    )


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    query: ViewTimelineQuery,
    *,
    audit: AuditEventStore,
    case_repo: CaseRepository,
    cursor_encoder: Any | None = None,
) -> TimelinePage:
    """Return a chronologically-ordered, paginated, redacted timeline.

    Failure behavior:
    - Missing or cross-tenant ``case_id`` -> ``NotFound`` (404).
    - Actor missing ``AUDIT_READ`` -> ``Forbidden`` (403).
    - Malformed filters or cursor -> ``ValidationFailed`` (422).

    ``cursor_encoder`` is the persistence-layer function the use case
    calls to produce the ``next_cursor``. Production wires
    ``encode_timeline_cursor`` from ``audit_outbox``; tests pass a stub.
    Decoupling the encoder from the persistence module keeps the use
    case importable from contexts where SQLAlchemy is unavailable.
    """
    _validate_query(query)

    # 1. Authorization. ``AUDIT_READ`` is the per-tenant permission; the
    # cross-tenant guard is the policy (Constitution V).
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.AUDIT_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )

    # 2. Existence + tenant scoping. A case in another tenant must
    # surface as 404, not as an empty timeline, so an unauthorized
    # actor cannot probe for cross-tenant ids.
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        raise NotFound(f"case {query.case_id} not found")

    # 3. Read the page. ``list_for_case`` enforces tenant + case
    # scoping internally; passing the actor's tenant_id here is
    # defense-in-depth (Constitution V).
    rows = await audit.list_for_case(
        tenant_id=query.tenant_id,
        case_id=query.case_id,
        event_types=list(query.event_types) if query.event_types else None,
        actor_id=query.actor_filter,
        since=query.since,
        until=query.until,
        limit=query.limit,
        cursor=query.cursor,
    )

    items = [_row_to_entry(r) for r in rows]

    # 4. Build the next_cursor. We always request exactly ``limit``
    # rows from the store; if we got ``limit`` rows back, there might
    # be more, so we emit a cursor anchored at the last row's
    # (occurred_at, id) tuple. If we got fewer than ``limit``, the
    # page is the last page.
    next_cursor: str | None = None
    if len(items) == query.limit and cursor_encoder is not None and rows:
        tail = rows[-1]
        next_cursor = cursor_encoder(tail.occurred_at, tail.id)

    return TimelinePage(items=items, next_cursor=next_cursor, limit=query.limit)


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "PAYLOAD_SAFE_KEYS",
    "TimelineEntry",
    "TimelinePage",
    "ViewTimelineQuery",
    "handle",
]
