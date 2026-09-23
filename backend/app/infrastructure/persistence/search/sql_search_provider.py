"""Portable LIKE-based search adapter (P06 / FM-031 / AGENTS.md §4.R1).

This is the local SQL implementation of ``SearchProvider``. It honors
the platform portability contract:

- Only SQLAlchemy generic types (``String``, ``JSON``).
- Only ``LIKE`` / case-insensitive equality for text matching. No
  ``ILIKE`` (PostgreSQL-only), no ``JSONB`` operators, no ``tsvector``,
  no ``gen_random_uuid()``. SQLite (tests) and PostgreSQL (local) both
  honor ``LIKE`` with the same semantics on ``String`` columns.
- Tenant scoping is enforced with an explicit ``WHERE tenant_id =
  :tenant_id`` on every query; the implementation never relies on the
  optional ``tenant_filter_listener`` for safety.

# INTEGRATION_SEAM: search engine

This adapter is the local implementation of the ``SearchProvider``
domain port. An Azure Cognitive Search / OpenSearch adapter would
replace this module without touching the application layer or the
router. The contract is the ``SearchSpec`` / ``SearchHit`` /
``SearchResultPage`` shapes in ``app/domain/ports.py``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ports import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.infrastructure.persistence.models import Case as CaseRow

# Core columns the application admits as filter fields. Each maps to a
# SQL column on the ``cases`` table. The application layer validates
# the request before we see it; we still guard against unknown keys
# defensively (a misconfigured caller could bypass validation).
_CORE_COLUMN_MAP: dict[str, Any] = {
    "reference": CaseRow.reference,
    "status": CaseRow.status,
    "priority": CaseRow.priority,
    "title": CaseRow.title,
    "current_workflow_state": CaseRow.current_workflow_state,
}

# Maximum number of searchable-field snippets projected into a single
# hit. The schema is bounded; this is a defensive ceiling so a
# misconfigured caller cannot force a multi-kilobyte payload.
_MAX_SNIPPET_KEYS = 32


class SqlSearchProvider(SearchProvider):
    """Portable ``LIKE`` search adapter.

    Why a separate class instead of a free function: the implementation
    is async on the ``AsyncSession`` and the application layer expects
    a ``SearchProvider`` instance. Keeping the constructor trivial
    makes it easy for tests to swap an in-memory fake.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def search(self, spec: SearchSpec) -> SearchResultPage:
        """Execute the search and return a paginated ``SearchResultPage``.

        The implementation order:

        1. Base ``SELECT`` over ``cases`` with the mandatory
           ``tenant_id`` filter.
        2. Free-text portion (``LIKE`` on ``title``).
        3. Per-filter clause. ``data.<key>`` filters run against the
           JSON TEXT representation via ``LOWER(cases.data) LIKE
           LOWER(:pattern)``; core-column filters run against the
           appropriate SQL column.
        4. Pagination via ``LIMIT`` / ``OFFSET``.
        5. Project the snippet: only ``searchable`` keys, with
           restricted values replaced by the ``Redacted`` marker when
           ``spec.can_see_sensitive`` is ``False``.
        """
        stmt = select(CaseRow).where(CaseRow.tenant_id == spec.tenant_id)

        text_query = (spec.text_query or "").strip()
        if text_query:
            # Why LIKE on title only: ``data`` is JSON; LIKEing into the
            # JSON TEXT for arbitrary free text is brittle across
            # dialects and bleeds false positives when the same string
            # appears as a value of an unrelated key. The free-text
            # surface is declared separately via filters.
            stmt = stmt.where(
                func.lower(CaseRow.title).like(f"%{_escape_like(text_query.lower())}%")
            )

        # Per-filter clauses. The application layer has already
        # rejected non-searchable fields; we still defensively check
        # every column against the allow-list.
        for f in spec.filters:
            stmt = _apply_filter(stmt, f, spec)

        # Total count of matching rows (advisory; useful for the UI
        # "X results" footer).
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int((await self._s.execute(count_stmt)).scalar_one())

        # Page slice.
        page_stmt = stmt.order_by(CaseRow.created_at.desc()).limit(spec.limit).offset(spec.offset)
        rows = (await self._s.execute(page_stmt)).scalars().all()

        items = [_row_to_hit(row, spec) for row in rows]
        next_cursor: str | None = None
        if len(items) == spec.limit:
            next_cursor = f"{spec.offset + spec.limit}:{spec.limit}"
        return SearchResultPage(
            items=items,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=next_cursor,
        )


def _apply_filter(stmt: Any, f: SearchFilter, spec: SearchSpec) -> Any:
    """Apply one ``SearchFilter`` to the SQLAlchemy ``select``.

    Maps the controlled operator vocabulary to portable clauses:

    - ``eq`` -> ``LOWER(col) = LOWER(:v)`` (case-insensitive equality).
    - ``like`` -> ``LOWER(col) LIKE LOWER(:v)`` (portable substring).
    - ``in`` -> ``LOWER(col) IN :vs`` (case-insensitive membership).

    For ``data.<key>`` filters we run against the JSON TEXT via
    ``LOWER(cases.data) LIKE LOWER(:pattern)`` where ``pattern`` is the
    exact ``"key":"value"`` substring. JSONB operators and JSON path
    expressions are explicitly avoided (AGENTS.md §4.R1).
    """
    if f.field.startswith("data."):
        return _apply_data_filter(stmt, f, spec)

    column = _CORE_COLUMN_MAP.get(f.field)
    if column is None:
        # The application layer should have rejected this; defensive
        # no-op so a misconfigured caller cannot crash the SQL run.
        return stmt

    if f.op == "eq":
        return stmt.where(func.lower(column) == func.lower(_as_str(f.value)))
    if f.op == "like":
        return stmt.where(func.lower(column).like(f"%{_escape_like(_as_str(f.value).lower())}%"))
    if f.op == "in":
        seq = f.value if isinstance(f.value, (list, tuple)) else ()
        lowered = [func.lower(_as_str(v)) for v in seq]
        return stmt.where(func.lower(column).in_(lowered))
    return stmt


def _apply_data_filter(stmt: Any, f: SearchFilter, spec: SearchSpec) -> Any:
    """Project a filter against a ``data.<key>`` field.

    The portable mechanism is a LIKE substring against the JSON TEXT
    representation: ``LOWER(cases.data) LIKE :pattern``. The pattern is
    built from the field key and the search value so the adapter
    matches either ``"key":"value"`` or ``"key": "value"`` shapes
    emitted by ``json.dumps(..., separators=(",", ":"))``.

    Why not ``json_extract`` / ``->>`` / path operators: those are
    dialect-specific (PostgreSQL JSONB and SQLite JSON1 differ in
    quoting, ordering, and escape semantics). Portable LIKE is the
    conservative, deterministic choice that satisfies the LIKE-not-JSONB
    acceptance criterion.
    """
    key = f.field[len("data.") :]
    # Restrict to declared searchable keys (defense-in-depth; the
    # application layer already enforces this).
    if key not in spec.searchable_field_keys:
        return stmt

    # Build a substring pattern. We match either ``"key":"value"``
    # (compact JSON) or ``"key": "value"`` (pretty JSON) so the same
    # pattern works for both ``json.dumps(d, separators=(",", ":"))``
    # and the default indented output.
    raw_value = f.value
    if f.op == "in":
        # For ``in`` we OR multiple LIKE clauses together; portable
        # across SQLite and PostgreSQL.
        clauses = [
            _data_like(key, _as_str(v)) for v in raw_value if isinstance(raw_value, (list, tuple))
        ]
        if not clauses:
            return stmt
        return stmt.where(or_(*clauses))
    if f.op == "eq":
        return stmt.where(_data_like_eq(key, _as_str(raw_value)))
    if f.op == "like":
        return stmt.where(_data_like_contains(key, _as_str(raw_value)))
    return stmt


def _data_like(key: str, value: str) -> Any:
    """LIKE substring matching ``"key":"value"`` JSON shapes (case-insensitive)."""
    pattern = f'%"{key}"%"{_escape_like(value)}"%'
    return func.lower(CaseRow.data).like(func.lower(pattern))


def _data_like_eq(key: str, value: str) -> Any:
    """Exact equality on ``"key":"value"`` JSON shape (case-insensitive)."""
    pattern = f'"{key}":"{value}"'
    return func.lower(CaseRow.data).like(func.lower(pattern))


def _data_like_contains(key: str, value: str) -> Any:
    """Substring match of ``value`` inside the ``"key":"...value..."`` JSON field."""
    pattern = f'%"{key}":%"{_escape_like(value)}"%'
    return func.lower(CaseRow.data).like(func.lower(pattern))


def _escape_like(value: str) -> str:
    """Escape ``%`` and ``_`` so user input cannot widen a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_str(value: Any) -> str:
    """Coerce a filter value to ``str`` for LIKE / equality matching."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _row_to_hit(row: CaseRow, spec: SearchSpec) -> SearchHit:
    """Project an ORM row into the wire-format ``SearchHit``.

    The snippet is bounded to ``spec.searchable_field_keys`` (the union
    declared by the actor's case-type schemas). Restricted values are
    replaced with the ``Redacted`` marker when the actor does not hold
    the sensitive-read grant. The JSON ``data`` column is parsed
    once via ``json.loads``; a malformed payload degrades to an empty
    snippet rather than crashing the search.
    """
    snippet: dict[str, Any] = {}
    try:
        payload = json.loads(row.data) if isinstance(row.data, str) else row.data
    except (ValueError, TypeError):
        payload = {}
    if isinstance(payload, dict):
        for key in spec.searchable_field_keys:
            if key in payload and key not in snippet:
                if len(snippet) >= _MAX_SNIPPET_KEYS:
                    break
                snippet[key] = payload[key]
    # Per-field redaction: any key declared sensitive gets masked
    # when the actor lacks the sensitive-read grant. The keys are the
    # case-type schema's contract, not the row's stored value. Each
    # marker carries the field key as ``ref`` (FM-011) so consumers
    # assert redaction by reference id without ever seeing content.
    if not spec.can_see_sensitive and spec.sensitive_field_keys:
        sensitive = set(spec.sensitive_field_keys)
        for key in list(snippet):
            if key in sensitive:
                snippet[key] = {"redacted": True, "reason": "sensitive", "ref": key}
    return SearchHit(
        case_id=row.id,
        reference=row.reference,
        priority=row.priority,
        status=row.status,
        title=row.title,
        data_snippet=snippet,
    )


__all__ = ["SqlSearchProvider"]
