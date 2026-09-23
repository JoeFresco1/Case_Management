"""Pagination envelope for list endpoints.

AGENTS.md §17.1 / plan.md:284 — every list endpoint paginates through
``Page[T]``. The contract is uniform so the frontend can write one
generic fetcher:

- ``items`` — the rows for the requested window.
- ``next_cursor`` — opaque string the client passes back to fetch the
  next page; ``None`` means there are no more rows.
- ``limit`` — the effective page size the server honored (echoed back
  so the client can confirm).

Why an explicit ``DEFAULT_LIMIT`` / ``MAX_LIMIT``: the contract is
fixed at the API layer; routers import these constants so a drift
between two endpoints can never expose different ceilings.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200


def normalize_limit(requested: int | None) -> int:
    """Clamp a caller-supplied page size to the contract.

    Why clamp silently rather than 422: a caller asking for more than the
    maximum is a soft contract violation, not a programming error;
    returning the maximum keeps the response shape stable. ``None`` and
    non-positive values fall back to ``DEFAULT_LIMIT``.
    """
    if requested is None or requested <= 0:
        return DEFAULT_LIMIT
    if requested > MAX_LIMIT:
        return MAX_LIMIT
    return requested


class Page[T](BaseModel):
    items: Sequence[T]
    next_cursor: str | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def build_page[T](
    items: Sequence[T],
    *,
    next_cursor: str | None,
    limit: int = DEFAULT_LIMIT,
) -> Page[T]:
    """Assemble a ``Page[T]`` enforcing the contract.

    Why a factory and not ``Page(items=..., next_cursor=...)`` at the
    call site: ``build_page`` re-applies ``normalize_limit`` so an
    endpoint that forgets to clamp cannot ship an out-of-range page.
    """
    return Page[T](items=list(items), next_cursor=next_cursor, limit=normalize_limit(limit))


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "Page", "build_page", "normalize_limit"]
