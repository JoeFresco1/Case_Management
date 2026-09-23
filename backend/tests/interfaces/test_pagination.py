"""Interface tests for the pagination envelope (AGENTS.md §17.1).

The contract under test (see ``interfaces/http/pagination.py``):

- ``DEFAULT_LIMIT = 50`` and ``MAX_LIMIT = 200`` are fixed at the API
  layer (plan.md:284). Two endpoints can never drift apart.
- ``normalize_limit`` clamps a caller-supplied page size: ``None`` and
  non-positive values fall back to ``DEFAULT_LIMIT``; anything above
  ``MAX_LIMIT`` is silently capped.
- ``build_page`` returns a ``Page[T]`` that always echoes the
  *effective* limit, even when the caller forgot to clamp.
- ``Page[T].limit`` itself is bounded by Pydantic ``ge=1, le=MAX_LIMIT``
  so a malformed payload cannot bypass the contract.

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.interfaces.http.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    build_page,
    normalize_limit,
)

# ---------------------------------------------------------------------------
# normalize_limit
# ---------------------------------------------------------------------------


def test_normalize_limit__none_falls_back_to_default() -> None:
    assert normalize_limit(None) == DEFAULT_LIMIT


def test_normalize_limit__zero_falls_back_to_default() -> None:
    assert normalize_limit(0) == DEFAULT_LIMIT


def test_normalize_limit__negative_falls_back_to_default() -> None:
    assert normalize_limit(-7) == DEFAULT_LIMIT


def test_normalize_limit__value_within_range_passes_through() -> None:
    assert normalize_limit(25) == 25


def test_normalize_limit__value_at_max_passes_through() -> None:
    assert normalize_limit(MAX_LIMIT) == MAX_LIMIT


def test_normalize_limit__value_above_max_is_capped() -> None:
    assert normalize_limit(MAX_LIMIT + 1) == MAX_LIMIT


def test_normalize_limit__extremely_large_value_is_capped() -> None:
    assert normalize_limit(10_000) == MAX_LIMIT


# ---------------------------------------------------------------------------
# build_page
# ---------------------------------------------------------------------------


def test_build_page__empty_items_produces_empty_page() -> None:
    page = build_page(items=[], next_cursor=None)
    assert page.items == []
    assert page.next_cursor is None
    assert page.limit == DEFAULT_LIMIT


def test_build_page__preserves_items_and_cursor() -> None:
    page = build_page(items=["a", "b"], next_cursor="opaque-token", limit=10)
    assert page.items == ["a", "b"]
    assert page.next_cursor == "opaque-token"
    assert page.limit == 10


def test_build_page__re_clamps_oversized_limit() -> None:
    page = build_page(items=[], next_cursor=None, limit=MAX_LIMIT + 50)
    assert page.limit == MAX_LIMIT


def test_build_page__re_clamps_zero_limit() -> None:
    page = build_page(items=[], next_cursor=None, limit=0)
    assert page.limit == DEFAULT_LIMIT


def test_build_page__re_clamps_negative_limit() -> None:
    page = build_page(items=[], next_cursor=None, limit=-1)
    assert page.limit == DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# Page model validation
# ---------------------------------------------------------------------------


def test_page__generic_type_carries_through() -> None:
    page: Page[int] = build_page(items=[1, 2, 3], next_cursor="c")
    assert page.items == [1, 2, 3]


def test_page__pydantic_rejects_zero_limit() -> None:
    with pytest.raises(ValidationError):
        Page[int](items=[], next_cursor=None, limit=0)


def test_page__pydantic_rejects_limit_above_max() -> None:
    with pytest.raises(ValidationError):
        Page[int](items=[], next_cursor=None, limit=MAX_LIMIT + 1)


def test_page__pydantic_serializes_to_expected_shape() -> None:
    page = build_page(items=[{"id": "abc"}], next_cursor=None, limit=5)
    dumped = page.model_dump()
    assert dumped == {"items": [{"id": "abc"}], "next_cursor": None, "limit": 5}


def test_page__constants_match_plan_contract() -> None:
    # AGENTS.md §17.1 / plan.md:284 — default 50, max 200.
    assert DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 200
