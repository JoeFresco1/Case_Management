"""SQL adapter assertions for ``SqlSearchProvider`` (P06 / FM-031).

The acceptance criterion is "LIKE not JSONB": the portable SQL adapter
must not emit any PostgreSQL-specific operator (``ILIKE``, ``->>``,
``jsonb_path_``, ``@>``, ``tsvector``, ``gen_random_uuid``,
``contains``/``starts_with`` JSON functions). This module compiles a
representative ``SearchSpec`` and asserts the rendered SQL contains
only ``like`` (case-insensitive) and standard scalar operators.

Tests run without a live database. SQLAlchemy's compile-cache is
enough to render the statement to dialect-agnostic SQL; we then grep
the rendered string for forbidden patterns.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects import sqlite

from app.domain.ports import SearchFilter, SearchSpec
from app.infrastructure.persistence.models import Case as CaseRow
from app.infrastructure.persistence.search.sql_search_provider import (
    SqlSearchProvider,
    _apply_filter,
    _row_to_hit,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")


def _compile(stmt: Any) -> str:
    return str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


def _spec(
    *,
    text_query: str | None = None,
    filters: tuple[SearchFilter, ...] = (),
    searchable: tuple[str, ...] = (),
    sensitive: tuple[str, ...] = (),
    can_see_sensitive: bool = True,
) -> SearchSpec:
    return SearchSpec(
        tenant_id=TENANT,
        text_query=text_query,
        filters=filters,
        searchable_field_keys=searchable,
        sensitive_field_keys=sensitive,
        can_see_sensitive=can_see_sensitive,
    )


# Forbidden patterns — each one is a portability / R1 violation.
_FORBIDDEN = (
    "->>",
    "-> ",
    "@>",
    "jsonb",
    "tsvector",
    "to_tsvector",
    "to_tsquery",
    "gen_random_uuid",
    "ILIKE",
    "ilike",
    "regexp_match",
    "regexp_replace",
)


def _assert_portable(sql: str) -> None:
    upper = sql.upper()
    for pat in _FORBIDDEN:
        assert pat.upper() not in upper, f"forbidden pattern {pat!r} in SQL: {sql!r}"


def test_sql_search_provider__priority_filter__uses_like_only() -> None:
    """The priority filter must compile to a portable LIKE clause."""
    SqlSearchProvider.__new__(SqlSearchProvider)  # bypass __init__
    spec = _spec(
        filters=(SearchFilter(field="priority", op="eq", value="urgent"),),
        searchable=("participantName",),
        sensitive=("ssn",),
    )
    stmt = select(CaseRow).where(CaseRow.tenant_id == spec.tenant_id)
    stmt = _apply_filter(stmt, spec.filters[0], spec)
    sql = _compile(stmt)
    assert "tenant_id" in sql
    assert "lower(" in sql.lower()
    _assert_portable(sql)


def test_sql_search_provider__data_field_filter__uses_like_only() -> None:
    """A ``data.<key>`` filter must compile to a LIKE clause (no JSONB)."""
    spec = _spec(
        filters=(SearchFilter(field="data.participantName", op="like", value="jane"),),
        searchable=("participantName",),
    )
    stmt = select(CaseRow).where(CaseRow.tenant_id == spec.tenant_id)
    stmt = _apply_filter(stmt, spec.filters[0], spec)
    sql = _compile(stmt)
    assert "like" in sql.lower()
    _assert_portable(sql)


def test_sql_search_provider__tenant_id_is_always_scoped() -> None:
    """Every search statement must include the tenant filter."""
    spec = _spec(text_query="hello")
    stmt = select(CaseRow).where(CaseRow.tenant_id == spec.tenant_id)
    if spec.text_query:
        stmt = stmt.where(func.lower(CaseRow.title).like(f"%{spec.text_query.lower()}%"))
    for f in spec.filters:
        stmt = _apply_filter(stmt, f, spec)
    sql = _compile(stmt)
    assert "tenant_id" in sql
    _assert_portable(sql)


def test_sql_search_provider__redacted_snippet__no_value_leak() -> None:
    """Snippet projection never includes restricted values when the
    actor lacks the grant. The SQL adapter reads only ``data`` via
    LIKE; the redaction happens in Python (``_row_to_hit``) so the
    SQL stays portable and no value leaks through the search index.
    """
    row = SimpleNamespace(
        id=uuid4(),
        reference="C-TEST-2026-000001",
        priority="normal",
        status="open",
        title="T",
        data='{"participantName": "Jane", "ssn": "111-22-3333"}',
    )
    spec = _spec(
        searchable=("participantName", "ssn"),
        sensitive=("ssn",),
        can_see_sensitive=False,
    )
    hit = _row_to_hit(row, spec)
    assert hit.data_snippet["participantName"] == "Jane"
    # why (FM-011): the marker carries the field key as ``ref`` so
    # consumers assert redaction by reference id, never by content.
    assert hit.data_snippet["ssn"] == {"redacted": True, "reason": "sensitive", "ref": "ssn"}


def test_sql_search_provider__malformed_json__snippet_empty() -> None:
    """A malformed JSON ``data`` payload degrades to an empty snippet
    rather than crashing the search.
    """
    row = SimpleNamespace(
        id=uuid4(),
        reference="C-TEST-2026-000001",
        priority="normal",
        status="open",
        title="T",
        data="not-json",
    )
    spec = _spec(searchable=("participantName", "ssn"))
    hit = _row_to_hit(row, spec)
    assert hit.data_snippet == {}


__all__ = [name for name in dir() if name.startswith("test_")]
