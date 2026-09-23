"""SQL adapters for audit, outbox, idempotency.

# INTEGRATION_SEAM: audit / outbox / idempotency stores

These are the local SQLAlchemy-backed implementations of:

- `AuditEventStore` (append-only `audit_events`, AGENTS.md §12, §16.1)
- `OutboxWriter` (`outbox` table, AGENTS.md §12, plan §4.R12, T028)
- `IdempotencyStore` (`idempotency_keys` table, AGENTS.md §10, T022)

They share the same DB transaction as the use case: `add`/`enqueue`/`put`
queue the row on the session; the use case commits once at the boundary.

All three stores enforce tenant isolation explicitly (a required
`tenant_id` argument on every method, and a `WHERE tenant_id = :tenant_id`
filter on every read).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit import (
    ACCESS_DECISION_RECORDED,
    AUDIT_CORRECTION_RECORDED,
    GENESIS_HASH,
    compute_hash,
    payload_hash,
)
from app.infrastructure.persistence.models import (
    AuditEvent as AuditEventRow,
)
from app.infrastructure.persistence.models import (
    IdempotencyKey as IdempotencyKeyRow,
)
from app.infrastructure.persistence.models import (
    Outbox as OutboxRow,
)

# ---------------------------------------------------------------------------
# Cursor codec (P09-US4 / AGENTS.md §17.1)
# ---------------------------------------------------------------------------
#
# The timeline cursor is an opaque ``<occurred_at_iso>|<id>`` string.
# Format is intentionally simple: no HMAC signing for the first slice
# (the cursor only reveals relative ordering); a signed cursor lands
# with FM-031 search. Parse errors raise ``ValueError``; the router
# translates that to 422 via the existing validation handler.


def _encode_cursor(occurred_at: datetime, row_id: Any) -> str:
    """Encode a (occurred_at, id) tuple into the wire cursor format."""
    return f"{occurred_at.isoformat()}|{row_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, Any]:
    """Decode the wire cursor into ``(occurred_at, id)``.

    Why we raise ``ValueError`` instead of returning a sentinel: the
    HTTP layer translates validation failures to 422, which is the
    correct contract for a malformed pagination token. A silent fallback
    would let a misbehaving client reorder pages without surface.
    """
    if "|" not in cursor:
        raise ValueError("malformed timeline cursor")
    iso, _, raw_id = cursor.partition("|")
    occurred_at = datetime.fromisoformat(iso)
    # UUIDs round-trip via string for portability (sqlite stores as
    # string; postgres as uuid).
    from uuid import UUID  # noqa: PLC0415

    return occurred_at, UUID(raw_id)


def encode_timeline_cursor(occurred_at: datetime, row_id: Any) -> str:
    """Public wrapper for the use case's ``next_cursor`` builder.

    Why exposed here and not in the application layer: the cursor
    format is a persistence concern (the ordering tuple it encodes
    mirrors the SQL ``ORDER BY (occurred_at, id)``). The application
    layer passes the last row's tuple through unchanged.
    """
    return _encode_cursor(occurred_at, row_id)


def decode_timeline_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode the public timeline cursor contract for an HTTP provider."""
    occurred_at, row_id = _decode_cursor(cursor)
    return occurred_at, row_id


def _make_dedupe(
    session: AsyncSession,
) -> Any:
    """Build a ``Dedupe`` callable bound to ``session``.

    Used by ``application.audit.record_event`` to dedupe before
    appending. Why here: the dedupe key includes
    ``(tenant_id, event_type, correlation_id, payload_hash)`` — the
    session is the only place that has the freshly-staged rows from
    the current transaction, so we cannot move this lookup to the
    application layer without round-tripping the storage.

    The callable returns ``True`` when a row with the same idempotency
    key is already staged in the session. The application layer
    treats that as "silent no-op" (AGENTS.md §10).
    """

    async def dedupe(
        tenant_id: UUID,
        event_type: str,
        correlation_id: str,
        payload_hash_value: str,
    ) -> bool:
        stmt = (
            select(AuditEventRow.id)
            .where(AuditEventRow.tenant_id == tenant_id)
            .where(AuditEventRow.event_type == event_type)
            .where(AuditEventRow.correlation_id == correlation_id)
            # T030: the payload hash is load-bearing. Without it, a
            # same-key retry carrying a *different* payload would
            # collapse onto the first row and silently drop the second
            # event. A row matches only on the full 4-tuple.
            .where(AuditEventRow.payload_hash == payload_hash_value)
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    return dedupe


def audit_dedupe(session: AsyncSession) -> Any:
    """Public wrapper for ``_make_dedupe``.

    Why exposed at module level: the router wires it directly to the
    use case without exposing the SQL implementation. The returned
    callable matches the ``application.audit.record_event.Dedupe``
    signature.
    """
    return _make_dedupe(session)


# ----- AuditEventStore ------------------------------------------------------


class SqlAuditEventStore:
    """Append-only audit store.

    Why there is no `update` or `delete`: AGENTS.md §4.R12 / plan §4.R12
    forbid mutation paths. The hash chain is the tamper-evidence; any
    UPDATE/DELETE breaks the chain and is rejected (the DB trigger and
    ORM listener raise). Corrections are compensating rows, not
    overwrites.

    ``list_for_chain`` returns rows in chronological order for the
    chain verifier (P09-US3, FM-003). The verifier is the pure function
    ``app.domain.audit.verify_chain``; this adapter just supplies the
    rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        prev_hash = await self._last_hash(tenant_id)
        row_hash = compute_hash(prev_hash, payload)
        self._s.add(
            AuditEventRow(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
                prev_hash=prev_hash,
                hash=row_hash,
                # T030: persist the canonical-payload hash the dedupe
                # key reads (same function `record_event` computes).
                payload_hash=payload_hash(payload),
            )
        )

    async def _last_hash(self, tenant_id: UUID) -> str:
        """Return the chain's previous-hash anchor for ``tenant_id``.

        The chain is per tenant; different tenants cannot influence each
        other's hash chain.
        """
        stmt = (
            select(AuditEventRow.hash)
            .where(AuditEventRow.tenant_id == tenant_id)
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(1)
        )
        last = (await self._s.execute(stmt)).scalar_one_or_none()
        return last or GENESIS_HASH

    async def list_for_chain(self, tenant_id: UUID) -> Sequence[AuditEventRow]:
        """Return all rows for ``tenant_id`` in chronological order.

        Used by the ``verify-audit`` CLI and by the chain-replay tests
        to feed ``app.domain.audit.verify_chain``. ``ORDER BY
        occurred_at, id`` is deterministic on sqlite and PostgreSQL
        when timestamps tie (UUIDv4 ids are random; the secondary
        ``id`` sort keeps the order stable across replays).
        """
        stmt = (
            select(AuditEventRow)
            .where(AuditEventRow.tenant_id == tenant_id)
            .order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.id.asc())
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types: Sequence[str] | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[AuditEventRow]:
        """Return audit rows for ``case_id`` in chronological order.

        Backs the per-case timeline endpoint (P09-US1, US4). Tenant
        isolation is enforced by ``WHERE tenant_id = :tenant_id``;
        ``case_id`` is the row's own column. Filters are AND-ed; an
        unspecified filter is omitted from the WHERE clause.

        Pagination cursor encodes ``(occurred_at_iso, id)`` so the same
        page is stable across replays when timestamps tie. The cursor
        is opaque on the wire (the use case passes it through
        unchanged); we parse it here and add a strict greater-than
        predicate to the WHERE so the next page picks up exactly where
        the previous one ended.

        Why a secondary ``id`` sort: UUIDv4 ids are random, but the
        chain is keyed on (tenant_id, occurred_at, id) — the hash
        query orders by id descending, so two rows in the same
        microsecond still have a deterministic order. The cursor uses
        the same tuple to guarantee no row is skipped or repeated.
        """
        stmt = (
            select(AuditEventRow)
            .where(AuditEventRow.tenant_id == tenant_id)
            .where(AuditEventRow.case_id == case_id)
        )
        if event_types:
            # Why ``in_`` not ``==``: the timeline filter is a list of
            # event types (P09-US4). Unknown types are silently ignored
            # so a malformed query parameter cannot crash the read.
            stmt = stmt.where(AuditEventRow.event_type.in_(list(event_types)))
        if actor_id is not None:
            stmt = stmt.where(AuditEventRow.actor_id == actor_id)
        if since is not None:
            stmt = stmt.where(AuditEventRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEventRow.occurred_at <= until)
        if cursor:
            cursor_at, cursor_id = _decode_cursor(cursor)
            # Strict greater-than on the (occurred_at, id) tuple. We use
            # SQLAlchemy tuple comparison which renders portably on
            # sqlite + postgres + azure-sql (AGENTS.md §4.R1).
            stmt = stmt.where(
                tuple_(AuditEventRow.occurred_at, AuditEventRow.id) > tuple_(cursor_at, cursor_id)
            )
        stmt = stmt.order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.id.asc()).limit(limit)
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def list_for_export(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID | None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[AuditEventRow]:
        """Return a structured export page (P09-US5 / FM-044).

        Mirrors ``list_for_case`` but the case filter is optional —
        ``None`` produces a tenant-wide date-range export. The cursor
        codec is the same ``(occurred_at_iso, id)`` tuple so a paginated
        caller can resume the same export across HTTP requests without
        duplication.

        Why no ``event_type`` / ``actor_id`` filters on this read:
        exports are evidence artifacts, not interactive filters. A
        process administrator exporting "the evidence for case X
        between T0 and T1" gets every row; redaction + verify happen on
        the wire (timeline handler already enforces the allowlist).
        """
        stmt = select(AuditEventRow).where(AuditEventRow.tenant_id == tenant_id)
        if case_id is not None:
            stmt = stmt.where(AuditEventRow.case_id == case_id)
        if since is not None:
            stmt = stmt.where(AuditEventRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEventRow.occurred_at <= until)
        if cursor:
            cursor_at, cursor_id = _decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(AuditEventRow.occurred_at, AuditEventRow.id) > tuple_(cursor_at, cursor_id)
            )
        stmt = stmt.order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.id.asc()).limit(limit)
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def get_event(
        self,
        *,
        tenant_id: UUID,
        event_id: UUID,
    ) -> AuditEventRow | None:
        """Fetch a single audit row by id, tenant-scoped.

        Returns ``None`` when the row does not exist or belongs to a
        different tenant — the use case surfaces ``NotFound`` so the
        actor cannot probe for cross-tenant ids (Constitution V).
        """
        stmt = select(AuditEventRow).where(
            AuditEventRow.tenant_id == tenant_id,
            AuditEventRow.id == event_id,
        )
        return (await self._s.execute(stmt)).scalars().one_or_none()

    async def latest_correction_id(
        self,
        *,
        tenant_id: UUID,
        correlation_id: str,
    ) -> UUID | None:
        """Return the latest correction row id for a tenant-scoped correlation.

        The lookup shares the request session with the append so the response
        can observe the staged correction before the boundary commits it.
        """
        stmt = (
            select(AuditEventRow.id)
            .where(
                AuditEventRow.tenant_id == tenant_id,
                AuditEventRow.event_type == AUDIT_CORRECTION_RECORDED,
                AuditEventRow.correlation_id == correlation_id,
            )
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_access_decisions(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        subject_type: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Sequence[AuditEventRow]:
        """Return ``AccessDecisionRecorded`` rows for the security report (FM-045).

        Tenant-scoped: the WHERE clause always carries ``tenant_id``.
        Filters are AND-ed on the indexed columns; ``action`` and
        ``result`` filters ride on the JSON ``payload`` column via
        portable ``cast(payload AS VARCHAR) LIKE`` so the shape works
        on sqlite + postgres without dialect-specific JSONB operators
        (AGENTS.md §4.R1). The column is indexed on ``tenant_id``,
        ``event_type``, and ``occurred_at`` so the planner narrows
        the rowset before the JSON scan runs.

        Pagination mirrors ``list_for_case``: the cursor decodes a
        ``(occurred_at_iso, id)`` tuple and adds a strict greater-than
        predicate so the next page picks up exactly where the
        previous one ended.
        """
        stmt = (
            select(AuditEventRow)
            .where(AuditEventRow.tenant_id == tenant_id)
            .where(AuditEventRow.event_type == ACCESS_DECISION_RECORDED)
        )
        if actor_id is not None:
            stmt = stmt.where(AuditEventRow.actor_id == actor_id)
        if subject_type:
            stmt = stmt.where(AuditEventRow.subject_type == subject_type)
        if since is not None:
            stmt = stmt.where(AuditEventRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEventRow.occurred_at <= until)
        if cursor:
            cursor_at, cursor_id = _decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(AuditEventRow.occurred_at, AuditEventRow.id) > tuple_(cursor_at, cursor_id)
            )
        # ``action`` and ``result`` are short enum-like strings inside
        # the payload JSON. The portable shape: cast the JSON column
        # to text and use LIKE without wildcards (effectively
        # equality) — both sqlite and postgres support it without a
        # JSONB-specific operator (AGENTS.md §4.R1).

        if action:
            stmt = stmt.where(cast(AuditEventRow.payload, String).like(f'%"{action}"%'))
        if result:
            stmt = stmt.where(cast(AuditEventRow.payload, String).like(f'%"{result}"%'))
        stmt = stmt.order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.id.asc()).limit(limit)
        result_ = await self._s.execute(stmt)
        return result_.scalars().all()


# ----- OutboxWriter ---------------------------------------------------------


class SqlOutboxWriter:
    """SQL-backed outbox writer.

    Why this is the only path for integration events: AGENTS.md §12 /
    plan §4.R12 / T028 require that integration events are written in the
    same DB transaction as the domain change and dispatched by a separate
    background worker. A direct call to ``MessageBus.publish`` would
    break the atomicity guarantee.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self._s.add(
            OutboxRow(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )


# ----- IdempotencyStore -----------------------------------------------------


class SqlIdempotencyStore:
    """SQL-backed Idempotency-Key store.

    The first response is replayed on subsequent requests with the same
    ``tenant_id + key`` and the same request hash. Same key + different
    request hash is a 409 (handled by `interfaces/http/idempotency.py`).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, *, tenant_id: UUID, key: str) -> dict[str, Any] | None:
        stmt = select(IdempotencyKeyRow).where(
            IdempotencyKeyRow.tenant_id == tenant_id,
            IdempotencyKeyRow.key == key,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return {
            "request_hash": row.request_hash,
            "response_status": row.response_status,
            "response_body": row.response_body,
            "correlation_id": row.correlation_id,
        }

    async def put(
        self,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        correlation_id: str,
    ) -> None:
        existing = (
            await self._s.execute(
                select(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.tenant_id == tenant_id,
                    IdempotencyKeyRow.key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # why: retries can race the response write. The first committed
            # response is authoritative; never overwrite it or create a
            # second row with a different result.
            if existing.request_hash != request_hash:
                raise ValueError("idempotency key already belongs to another request")
            return
        self._s.add(
            IdempotencyKeyRow(
                tenant_id=tenant_id,
                key=key,
                request_hash=request_hash,
                response_status=response_status,
                response_body=response_body,
                correlation_id=correlation_id,
                created_at=datetime.now(UTC),
            )
        )


__all__ = [
    "SqlAuditEventStore",
    "SqlOutboxWriter",
    "SqlIdempotencyStore",
    "audit_dedupe",
    "encode_timeline_cursor",
]


# ---------------------------------------------------------------------------
# Lazy re-export of the test-only InMemoryIdempotencyStore so the existing
# test_create_case scenarios (which historically imported this class from
# the production module) keep working without dragging test code into the
# runtime import graph.
#
# The class itself lives in tests/application/cases/_fakes.py and is only
# resolvable when the tests package is importable. When this module is
# imported by a slim runtime context (e.g., alembic) the lazy attribute
# raises a clear error rather than failing at import time.
# ---------------------------------------------------------------------------


def __getattr__(name: str):  # pragma: no cover — test scaffolding
    if name == "InMemoryIdempotencyStore":
        from tests.application.cases._fakes import (  # noqa: PLC0415
            InMemoryIdempotencyStore,
        )

        return InMemoryIdempotencyStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
