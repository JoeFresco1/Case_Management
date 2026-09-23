"""Shared reports router, Idempotency-Key guard, and domain-error translation.

# Why this module (R2 structural split)

The former single-file ``reports.py`` module (722 lines) passed the R2
600-line soft limit. This module holds the pieces every route group in
the package shares:

- :data:`router` — the one ``APIRouter(prefix="/reports",
  tags=["reports"])`` that the route modules (:mod:`operational`,
  :mod:`custom`, and :mod:`dashboard`) register onto. Registration
  order across those modules is irrelevant: no two report paths overlap
  at the same segment depth **and** method, so Starlette's first-match
  rule resolves identically regardless of import order.
- :func:`_require_idempotency_key` — router-boundary enforcement of the
  ``Idempotency-Key`` contract for the write-once custom-report
  mutations.
- :func:`_translate_error` — domain-error → HTTP mapping used by the
  operational snapshot reads and the dashboard.
- :func:`_translate_not_found` — domain-error → HTTP mapping used by
  the custom-report define / render / export endpoints (the 404/422
  variant).

# INTEGRATION_SEAM: report engine (HTTP entry)

This package is the only HTTP entry to the operational reports and
custom-report designer surface (the endpoint inventory lives in the
package docstring of :mod:`app.interfaces.http.routers.reports`). The
handlers delegate the business decision to the application layer under
``app.application.reports``; this module only owns the shared ``router``
and the error-translation contract. The Pydantic schemas the routes
project onto are the OpenAPI contract — hand-editing fields at a route
is forbidden (AGENTS.md §4.R7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.domain.errors import (
    DependencyUnavailable,
    Forbidden,
    NotFound,
    ValidationFailed,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def _require_idempotency_key(value: str | None) -> str:
    """Enforce the ``Idempotency-Key`` contract at the router boundary.

    The global :class:`IdempotencyMiddleware` replays transport retries,
    but router-level enforcement keeps the contract explicit in OpenAPI
    and deterministic under test clients that disable the middleware.
    A missing/blank key is 400 so the mutation never reaches the use case.

    Args:
        value: Raw ``Idempotency-Key`` header value (may be absent).

    Returns:
        The trimmed key.

    Raises:
        HTTPException: 400 when the key is missing, blank, or longer
            than 128 characters.
    """
    if not value or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if len(value.strip()) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is too long",
        )
    return value.strip()


def _translate_error(exc: Exception) -> HTTPException:
    """Translate a domain error into the matching HTTP response.

    Centralized here so both snapshot endpoints and the dashboard behave
    identically. ``Forbidden`` is 403 (Constitution V); ``ValidationFailed``
    is 400; ``DependencyUnavailable`` is 503; anything else bubbles up to
    the global handler.

    Args:
        exc: Domain error raised by an application use case.

    Returns:
        The matching ``HTTPException``.

    Raises:
        Exception: re-raised unchanged when the error has no HTTP mapping.
    """
    if isinstance(exc, Forbidden):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": str(exc)},
        )
    if isinstance(exc, ValidationFailed):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "validation_failed", "message": str(exc), "field": exc.field},
        )
    if isinstance(exc, DependencyUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "dependency_unavailable", "message": str(exc)},
        )
    raise exc


def _translate_not_found(exc: Exception) -> HTTPException:
    """Translate a domain error into the matching HTTP response.

    Centralized here so all three custom-report endpoints behave
    identically. ``Forbidden`` is 403 (Constitution V);
    ``ValidationFailed`` is 422; ``NotFound`` is 404;
    ``DependencyUnavailable`` is 503; anything else bubbles up to
    the global handler.

    Args:
        exc: Domain error raised by an application use case.

    Returns:
        The matching ``HTTPException``.

    Raises:
        Exception: re-raised unchanged when the error has no HTTP mapping.
    """
    if isinstance(exc, Forbidden):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": str(exc)},
        )
    if isinstance(exc, ValidationFailed):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "validation_failed", "message": str(exc), "field": exc.field},
        )
    if isinstance(exc, NotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        )
    if isinstance(exc, DependencyUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "dependency_unavailable", "message": str(exc)},
        )
    raise exc
