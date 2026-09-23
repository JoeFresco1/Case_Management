"""Permissioned-bundle + share-by-link router (T5-US3).

# INTEGRATION_SEAM: HTTP entry

Bundle grants confer read access *through the bundle only*; the
content-search path (``application.search.document_search``) never
takes a bundle as a corpus, so membership never makes bytes
searchable to a non-member (acceptance: "bundle content not
searchable by content by non-members").

Share-link denials carry a bare reason code only (``unknown`` /
``expired`` / ``revoked`` / ``exhausted``) — a guessed-URL probe
learns nothing about the bundle behind the link.

Idempotency: every successful mutation is recorded under the
``Idempotency-Key`` header (AGENTS.md §10).
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.documents import doc_ops as doc_ops_uc
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.doc_bundle import BundleRole
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.doc_bundles import (
    BundleResponse,
    ChangeBundleMembersRequest,
    CreateBundleRequest,
    GrantBundleAccessRequest,
    MintLinkRequest,
    MintLinkResponse,
    RedeemLinkRequest,
    RedeemLinkResponse,
    RevokeBundleRequest,
    RevokeLinkRequest,
    bundle_to_response,
    link_to_mint_response,
)

router = APIRouter(prefix="/bundles", tags=["document-bundles"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def get_bundle_store(request: Request) -> doc_ops_uc.BundleStore:
    """Return the bundle store selected by the composition root."""
    store = getattr(request.app.state, "bundle_store", None)
    if store is None:
        raise RuntimeError("bundle store is not configured")
    return cast(doc_ops_uc.BundleStore, store)


def get_link_store(request: Request) -> doc_ops_uc.LinkStore:
    """Return the share-link store selected by the composition root."""
    store = getattr(request.app.state, "link_store", None)
    if store is None:
        raise RuntimeError("link store is not configured")
    return cast(doc_ops_uc.LinkStore, store)


def _require_idempotency_key(idempotency_key: str | None) -> None:
    """Reject mutations without an ``Idempotency-Key`` header (FR-005)."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )


# ---------------------------------------------------------------------------
# POST /api/v1/bundles (create) + GET (read) + PATCH members + POST revoke
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=BundleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a permissioned document bundle",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Member document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Blank name or bad members"},
    },
)
async def create_bundle(
    body: CreateBundleRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BundleResponse:
    """Create a bundle over tenant-scoped documents (owner = creator)."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    bundle = await doc_ops_uc.create_bundle(
        doc_ops_uc.CreateBundleCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            name=body.name,
            member_document_ids=tuple(body.member_document_ids),
        ),
        document_repo=repositories.document,
        bundle_store=bundle_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return bundle_to_response(bundle)


@router.get(
    "/{bundle_id}",
    response_model=BundleResponse,
    summary="Read a bundle's metadata (granted actors only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not granted, revoked, or cross-tenant"},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle not found in tenant"},
    },
)
async def read_bundle(
    bundle_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
) -> BundleResponse:
    """Return bundle metadata iff the actor holds VIEWER (or owns it)."""
    from app.domain.doc_bundle import check_bundle_access  # noqa: PLC0415

    repositories = dependencies.for_session(db)
    bundle = await bundle_store.get(user.tenant_id, bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"bundle {bundle_id} not found"},
        )
    grants = await bundle_store.get_grants(user.tenant_id, bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=user.tenant_id,
        actor_id=user.user_id,
        grants=grants,
        required=BundleRole.VIEWER,
    )
    _ = repositories
    return bundle_to_response(bundle)


@router.post(
    "/{bundle_id}/grants",
    status_code=status.HTTP_200_OK,
    summary="Grant a bundle role to another actor (editor only)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Granter lacks editor access"},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle not found in tenant"},
    },
)
async def grant_access(
    bundle_id: UUID,
    body: GrantBundleAccessRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str]:
    """Grant ``body.role`` on the bundle to ``body.grantee_id``."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    await doc_ops_uc.grant_bundle_access(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        bundle_id=bundle_id,
        grantee_id=body.grantee_id,
        role=body.role,
        bundle_store=bundle_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return {"bundle_id": str(bundle_id), "grantee_id": str(body.grantee_id)}


@router.patch(
    "/{bundle_id}/members",
    response_model=BundleResponse,
    summary="Add/remove bundle members (editor only)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks editor access"},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle or document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Frozen bundle or cap exceeded"},
    },
)
async def change_members(
    bundle_id: UUID,
    body: ChangeBundleMembersRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BundleResponse:
    """Apply membership edits; removal cuts the bundle read path at once."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    updated = await doc_ops_uc.change_bundle_members(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        bundle_id=bundle_id,
        added=tuple(body.added),
        removed=tuple(body.removed),
        document_repo=repositories.document,
        bundle_store=bundle_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return bundle_to_response(updated)


@router.post(
    "/{bundle_id}/revoke",
    response_model=BundleResponse,
    summary="Revoke a bundle (irreversible; kills grants + links)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks editor access"},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle not found in tenant"},
    },
)
async def revoke_bundle(
    bundle_id: UUID,
    body: RevokeBundleRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BundleResponse:
    """Revoke a bundle; all bound links die implicitly."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    revoked = await doc_ops_uc.revoke_bundle(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        bundle_id=bundle_id,
        reason=body.reason,
        bundle_store=bundle_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return bundle_to_response(revoked)


# ---------------------------------------------------------------------------
# Share-by-link: mint / redeem / revoke
# ---------------------------------------------------------------------------


@router.post(
    "/{bundle_id}/links",
    response_model=MintLinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a share-by-link bearer token for a bundle",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Minter lacks bundle viewer access"},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "TTL outside 5m..30d"},
    },
)
async def mint_link(
    bundle_id: UUID,
    body: MintLinkRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    link_store: doc_ops_uc.LinkStore = Depends(get_link_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MintLinkResponse:
    """Mint a bearer link; the raw token is returned exactly once."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    minted = await doc_ops_uc.mint_share_link(
        doc_ops_uc.MintLinkCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            bundle_id=bundle_id,
            ttl=timedelta(seconds=body.ttl_seconds),
            max_uses=body.max_uses,
        ),
        bundle_store=bundle_store,
        link_store=link_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return link_to_mint_response(minted.link, raw_token=minted.raw_token)


@router.post(
    "/links/redeem",
    response_model=RedeemLinkResponse,
    summary="Redeem a share-link bearer token (no auth required)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Unknown, expired, revoked, or exhausted link"},
    },
)
async def redeem_link(
    body: RedeemLinkRequest,
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    link_store: doc_ops_uc.LinkStore = Depends(get_link_store),
) -> RedeemLinkResponse:
    """Present a bearer token; returns the bundle's member ids (no bytes).

    The tenant scope comes from the link row itself: redemption is
    bearer-authorized, so no ``UserContext`` is required — the token
    *is* the credential. The tenant is resolved by digest lookup
    across the single-tenant test scope via the request's link store.
    """
    repositories = dependencies.for_session(db)
    # Why the tenant comes from the stored link: bearer redemption is
    # deliberately unauthenticated (the token is the credential), so
    # there is no actor tenant to scope by. The lookup below tries the
    # digest without a tenant filter via a store scan — the in-memory
    # adapter supports it; the SQL adapter (later slice) resolves the
    # tenant from the link row's own ``tenant_id`` column.
    scan = getattr(link_store, "scan_by_digest", None)
    link = await scan(body.raw_token) if scan is not None else None
    if link is None:
        from app.domain.doc_bundle import check_link_unknown  # noqa: PLC0415

        raise check_link_unknown()
    redeemed = await doc_ops_uc.redeem_share_link(
        tenant_id=link.tenant_id,
        raw_token=body.raw_token,
        bundle_store=bundle_store,
        link_store=link_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return RedeemLinkResponse(
        bundle_id=redeemed.bundle.id,
        member_document_ids=list(redeemed.member_document_ids),
    )


@router.post(
    "/links/revoke",
    status_code=status.HTTP_200_OK,
    summary="Revoke a share link by its token (bundle viewer only)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Unknown link or no bundle access"},
    },
)
async def revoke_link(
    body: RevokeLinkRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    bundle_store: doc_ops_uc.BundleStore = Depends(get_bundle_store),
    link_store: doc_ops_uc.LinkStore = Depends(get_link_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str]:
    """Revoke a link; the bearer token stops authorizing at once."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    revoked = await doc_ops_uc.revoke_share_link(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        raw_token=body.raw_token,
        reason=body.reason,
        bundle_store=bundle_store,
        link_store=link_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return {"link_id": str(revoked.id), "bundle_id": str(revoked.bundle_id)}


__all__ = ["get_bundle_store", "get_link_store", "router"]
