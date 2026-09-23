"""Pydantic schemas for the T5 permissioned-bundle + share-by-link API (T5-US3).

Bundle reads are metadata-only (names, member ids, grant roles); raw
share tokens appear exactly once in the mint response and are never
stored, logged, or re-emitted.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.doc_bundle import BundleRole, DocBundle, ShareLink


class CreateBundleRequest(BaseModel):
    """Body for ``POST /api/v1/bundles``."""

    name: str = Field(min_length=1, max_length=200)
    member_document_ids: list[UUID] = Field(default_factory=list, max_length=500)


class ChangeBundleMembersRequest(BaseModel):
    """Body for ``PATCH /api/v1/bundles/{id}/members``."""

    added: list[UUID] = Field(default_factory=list)
    removed: list[UUID] = Field(default_factory=list)


class RevokeBundleRequest(BaseModel):
    """Body for ``POST /api/v1/bundles/{id}/revoke``."""

    reason: str = Field(min_length=1, max_length=500)


class GrantBundleAccessRequest(BaseModel):
    """Body for ``POST /api/v1/bundles/{id}/grants``."""

    grantee_id: UUID
    role: BundleRole


class BundleResponse(BaseModel):
    """The wire shape of a permissioned bundle (metadata only)."""

    id: UUID
    tenant_id: UUID
    name: str
    member_document_ids: list[UUID]
    status: str
    created_by: UUID


def bundle_to_response(bundle: DocBundle) -> BundleResponse:
    """Translate a ``DocBundle`` aggregate to its wire shape."""
    return BundleResponse(
        id=bundle.id,
        tenant_id=bundle.tenant_id,
        name=bundle.name,
        member_document_ids=list(bundle.member_document_ids),
        status=bundle.status,
        created_by=bundle.created_by,
    )


class MintLinkRequest(BaseModel):
    """Body for ``POST /api/v1/bundles/{id}/links``."""

    ttl_seconds: int = Field(ge=300, le=30 * 24 * 3600)
    max_uses: int = Field(ge=1)


class MintLinkResponse(BaseModel):
    """The mint response: link metadata plus the one-time raw token."""

    link_id: UUID
    bundle_id: UUID
    expires_at: str
    max_uses: int
    raw_token: str


def link_to_mint_response(link: ShareLink, *, raw_token: str) -> MintLinkResponse:
    """Translate a minted link + its one-time token to wire shape."""
    return MintLinkResponse(
        link_id=link.id,
        bundle_id=link.bundle_id,
        expires_at=link.expires_at.isoformat(),
        max_uses=link.max_uses,
        raw_token=raw_token,
    )


class RedeemLinkRequest(BaseModel):
    """Body for ``POST /api/v1/bundles/links/redeem`` (bearer use)."""

    raw_token: str = Field(min_length=16)


class RedeemLinkResponse(BaseModel):
    """A successful redemption: the bundle's member ids (no bytes)."""

    bundle_id: UUID
    member_document_ids: list[UUID]


class RevokeLinkRequest(BaseModel):
    """Body for ``POST /api/v1/bundles/links/revoke``."""

    raw_token: str = Field(min_length=16)
    reason: str = Field(min_length=1, max_length=500)


__all__ = [
    "BundleResponse",
    "ChangeBundleMembersRequest",
    "CreateBundleRequest",
    "GrantBundleAccessRequest",
    "MintLinkRequest",
    "MintLinkResponse",
    "RedeemLinkRequest",
    "RedeemLinkResponse",
    "RevokeBundleRequest",
    "RevokeLinkRequest",
    "bundle_to_response",
    "link_to_mint_response",
]
