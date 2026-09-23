"""Pydantic request/response schemas for the evidence bundle surface.

The endpoint takes a small JSON body (``case_id``, ``reason``, optional
``can_see_sensitive``) and returns a streaming ZIP archive plus a
``Content-Disposition`` header naming the file. The schema does not
model the archive itself: the wire contract is "ZIP file,
``application/zip``" and the body shape is enforced by the
application-layer use case.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class EvidenceBundleRequest(BaseModel):
    """Request body for ``POST /api/v1/exports/bundle`` (P14-US8 / FM-060).

    ``case_id`` identifies the case the operator is exporting.
    ``reason`` is a free-text justification that lands on the audit
    chain verbatim — same convention as the structured audit export
    (FM-044). ``can_see_sensitive`` is an explicit opt-in: the
    default is ``False`` so the bundle ships with restricted fields
    redacted; the use case re-evaluates ``CASE_READ_SENSITIVE``
    against the actor's role set so a request without the grant is
    refused regardless of the flag.
    """

    case_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    can_see_sensitive: bool = False


__all__ = ["EvidenceBundleRequest"]
