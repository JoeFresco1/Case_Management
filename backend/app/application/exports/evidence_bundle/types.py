"""Command and result types for the portable evidence bundle (P14-US8 / FM-060).

The three public dataclasses are the use case's I/O contract: the input
command, a single archive entry ready for streaming, and the aggregate
result the HTTP router adapts into a ``StreamingResponse``. Split out of
the former single ``evidence_bundle`` module by the R2 structural
refactor; everything here is re-exported from
``app.application.exports.evidence_bundle`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildEvidenceBundleCommand:
    """The input to :func:`handle`.

    ``can_see_sensitive`` defaults to ``False`` so the bundle's
    default posture is "restricted fields are redacted". An actor
    who already holds ``CASE_READ_SENSITIVE`` can request the
    unredacted view by passing ``True``; the use case re-evaluates
    the action through :func:`authorize` so a request without the
    grant is refused regardless of the flag.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    reason: str
    can_see_sensitive: bool = False
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceBundleEntry:
    """A single archive entry ready to be written to the ZIP.

    The router translates this list into a streamable
    :class:`fastapi.responses.StreamingResponse` so the response
    never holds the full archive in memory beyond what the ZIP
    builder needs to flush.
    """

    name: str
    data: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """The result of :func:`handle`.

    ``entries`` are the archive members in the order the router
    should write them. ``manifest_hash`` is the SHA-256 over the
    canonical-JSON ``manifest.json`` payload so a downstream tool
    can verify the manifest without unpacking everything. ``archive``
    is the pre-built ZIP bytes for callers that want a single
    in-memory blob (CLI, tests). The router prefers ``entries`` so
    the response streams without buffering the full archive.
    """

    entries: tuple[EvidenceBundleEntry, ...]
    manifest_hash: str
    archive: bytes
    document_count: int
    audit_row_count: int


__all__ = [
    "BuildEvidenceBundleCommand",
    "EvidenceBundle",
    "EvidenceBundleEntry",
    "UUID",
    "dataclass",
]
