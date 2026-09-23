"""Shared routers, document-store dependency, and header-safety helpers.

# Why this module (R2 structural split)

The former single-file ``documents.py`` module (872 lines) passed the
R2 600-line soft limit. This module owns the pieces every route group
in the package shares:

- :data:`router` — ``APIRouter(prefix="/cases", tags=["documents"])``
  that the case-scoped upload route (:mod:`upload`) registers onto.
- :data:`versions_router` — ``APIRouter(prefix="/documents",
  tags=["documents"])`` that the document-scoped content / version /
  protection routes (:mod:`content`, :mod:`versions`,
  :mod:`protection`) register onto. ``app.main`` imports both routers
  from the package ``__init__``, so the consumer contract is unchanged.
- :func:`get_document_store` — resolves the ``DocumentStore`` seam off
  ``app.state`` (AGENTS.md §12). ``routers/viewer.py`` imports it from
  here; adapters swap in by rebinding ``app.state`` at composition time
  with no router change.
- the router-level size backstop (:func:`_router_max_size`) and the
  response-header safety helpers (:func:`_content_disposition`,
  :func:`_safe_content_type`) used by the download and upload routes.

Registration order across the sibling route modules is irrelevant: no
two document paths overlap at the same segment depth and method, so
Starlette's first-match rule resolves identically regardless of import
order.

# INTEGRATION_SEAM: document store (HTTP entry)

:func:`get_document_store` reads ``app.state.document_store`` — the
local adapter is ``infrastructure/storage/local_document_store.py``;
the Azure production adapter (``AzureBlobDocumentStore``) plugs in
behind the same ``DocumentStore`` port at composition time.
"""

from __future__ import annotations

import re
from typing import cast

from fastapi import APIRouter, Request

from app.application.documents import upload_document as upload_document_uc
from app.domain.ports import DocumentStore

router = APIRouter(prefix="/cases", tags=["documents"])
versions_router = APIRouter(prefix="/documents", tags=["documents"])


def get_document_store(request: Request) -> DocumentStore:
    """Return the document store selected by the composition root.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured ``DocumentStore`` implementation.

    Raises:
        RuntimeError: if the app was composed without a document store
            (a wiring bug — fail fast rather than serve half-answers).
    """
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        raise RuntimeError("document store is not configured")
    return cast(DocumentStore, store)


def _router_max_size() -> int:
    """The router-level pre-check size limit.

    The use case applies the case-type-specific limit; this default is
    a global backstop that protects against an unbounded upload before
    the policy lookup runs.
    """
    # Why a static default: the use case is the canonical place for
    # policy enforcement. The router pre-check uses the same default
    # so both layers agree.
    return upload_document_uc.DEFAULT_MAX_SIZE_BYTES


def _content_disposition(file_name: str) -> str:
    """Build an attachment header without permitting header injection."""
    safe_name = re.sub(r"[\x00-\x1f\x7f\\/]", "_", file_name).replace('"', "'")
    return f'attachment; filename="{safe_name}"'


def _safe_content_type(content_type: str) -> str:
    """Return a metadata content type only when it is safe for a header."""
    value = content_type.strip()
    if (
        value
        and not re.search(r"[\r\n]", value)
        and re.fullmatch(r"[^\s;,]+(?:\s*;\s*[^\r\n]+)?", value)
    ):
        return value
    return "application/octet-stream"
