"""Shared viewer router and idempotency-key guard.

# Why this module (R2 structural split)

The former single-file ``viewer.py`` module (653 lines) passed the R2
600-line soft limit. This module owns the two pieces every route group
in the package shares:

- :data:`router` — the single ``APIRouter(prefix="/documents",
  tags=["viewer"])`` that :mod:`annotation`, :mod:`renditions`,
  :mod:`checkout`, and :mod:`reindex` all register their routes onto,
  so ``app.main`` keeps importing one ``router`` from the package
  ``__init__`` with the identical route set.
- :func:`_require_idempotency_key` — the 400 guard every mutating
  viewer endpoint calls before touching the use cases
  (AGENTS.md §10: ``Idempotency-Key`` header required).

Registration order across the sibling route modules is irrelevant: no
two viewer paths overlap at the same segment depth and method, so
Starlette's first-match rule resolves identically regardless of import
order.

# INTEGRATION_SEAM: HTTP entry

The viewer pipeline is an overlay model: annotations are markup pinned
to ``(document_id, document_version, page)`` and never mutate document
bytes. This module holds no business logic — only routing scaffolding.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/documents", tags=["viewer"])


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """Refuse mutations without an ``Idempotency-Key`` (AGENTS.md §10).

    Args:
        idempotency_key: Raw ``Idempotency-Key`` header value, if any.

    Returns:
        The stripped key to echo into the idempotency middleware.

    Raises:
        HTTPException: 400 when the header is missing or blank —
            fail-closed so a retry-unsafe mutation never runs.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    return idempotency_key.strip()


__all__ = ["_require_idempotency_key", "router"]
