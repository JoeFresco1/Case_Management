"""Pydantic schemas for the T5 document-operations API (x-refs, separation, packages, dedup, CSV).

All request/response shapes are versioned under ``/api/v1``. UUIDs are
native ``UUID`` fields (FastAPI serializes them); timestamps render as
ISO 8601 strings to match the rest of the API.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.doc_ops import DocXref


class CreateXrefRequest(BaseModel):
    """Body for ``POST /api/v1/documents/{id}/xrefs``."""

    target_document_id: UUID
    label: str = Field(min_length=1, max_length=120)
    sort_order: int = Field(default=0, ge=0)


class XrefResponse(BaseModel):
    """The wire shape of one cross-document reference."""

    id: UUID
    tenant_id: UUID
    source_document_id: UUID
    target_document_id: UUID
    label: str
    sort_order: int
    created_by: UUID
    created_at: str


def xref_to_response(xref: DocXref) -> XrefResponse:
    """Translate a ``DocXref`` aggregate to its wire shape."""
    return XrefResponse(
        id=xref.id,
        tenant_id=xref.tenant_id,
        source_document_id=xref.source_document_id,
        target_document_id=xref.target_document_id,
        label=xref.label,
        sort_order=xref.sort_order,
        created_by=xref.created_by,
        created_at=xref.created_at.isoformat(),
    )


class PageRangeRequest(BaseModel):
    """One 1-based inclusive page interval for a split plan."""

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class PlanSplitRequest(BaseModel):
    """Body for ``POST /api/v1/documents/{id}/separation/split``."""

    case_id: UUID
    page_count: int = Field(ge=1)
    ranges: list[PageRangeRequest] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class PlanJoinMergeRequest(BaseModel):
    """Body for ``POST /api/v1/documents/separation/join-merge``."""

    case_id: UUID
    operation: str
    source_document_ids: list[UUID] = Field(min_length=2)
    page_counts: list[int] = Field(min_length=2)
    reason: str = Field(min_length=1, max_length=500)


class SeparationPlanResponse(BaseModel):
    """The wire shape of a validated separation plan (audit record)."""

    separation_id: str
    operation: str
    sources: list[str]
    source_versions: list[int]
    outputs: list[str]
    page_ranges: list[dict[str, int]]


class BuildPackageRequest(BaseModel):
    """Body for ``POST /api/v1/documents/packages``."""

    name: str = Field(min_length=1, max_length=200)
    member_document_ids: list[UUID] = Field(min_length=1, max_length=500)


class PackageManifestResponse(BaseModel):
    """The wire shape of a built package: manifest first (acceptance)."""

    manifest: dict[str, object]


class DedupResponse(BaseModel):
    """Groups of document-id strings sharing one content hash."""

    groups: list[list[str]]


class ExportCsvRequest(BaseModel):
    """Body for ``POST /api/v1/cases/{id}/documents/csv-export``."""

    columns: list[str] = Field(min_length=1)


__all__ = [
    "BuildPackageRequest",
    "CreateXrefRequest",
    "DedupResponse",
    "ExportCsvRequest",
    "PackageManifestResponse",
    "PageRangeRequest",
    "PlanJoinMergeRequest",
    "PlanSplitRequest",
    "SeparationPlanResponse",
    "XrefResponse",
    "xref_to_response",
]
