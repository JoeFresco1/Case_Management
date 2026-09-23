"""Permissioned bundles + share-by-link use cases (T5-US3).

Bundle grants confer read access *through the bundle only*; the
underlying document grants never change, so removal / revocation
closes the path immediately with no fan-out. Bearer links carry
only a SHA-256 digest server-side; the raw token exists solely in
the mint response. Content search never takes a bundle as a corpus.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.doc_bundle import (
    BundleRole,
    DocBundle,
    ShareLink,
    ShareLinkDenialReason,
    check_bundle_access,
    check_link_unknown,
    hash_token,
)
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

from ._common import (
    DOC_BUNDLE_CREATED,
    DOC_BUNDLE_MEMBERS_CHANGED,
    DOC_BUNDLE_REVOKED,
    SHARE_LINK_MINTED,
    SHARE_LINK_REDEEMED,
    SHARE_LINK_REVOKED,
    SUBJECT_TYPE_BUNDLE,
    append_audit_and_outbox,
    has_action,
    proxy,
    resolve_document,
)


class BundleStore(Protocol):
    """Persistence port for :class:`DocBundle` + per-actor grants."""

    async def add(self, bundle: DocBundle) -> None:
        """Persist a freshly created bundle row."""
        ...

    async def get(self, tenant_id: UUID, bundle_id: UUID) -> DocBundle | None:
        """Return one bundle, or ``None`` (missing or cross-tenant)."""
        ...

    async def save(self, bundle: DocBundle) -> None:
        """Persist membership/revocation transitions on a bundle row."""
        ...

    async def get_grants(self, tenant_id: UUID, bundle_id: UUID) -> Mapping[UUID, BundleRole]:
        """Return the live per-actor grant map for a bundle."""
        ...

    async def save_grant(
        self, tenant_id: UUID, bundle_id: UUID, actor_id: UUID, role: BundleRole
    ) -> None:
        """Grant ``role`` on a bundle to ``actor_id`` (upsert)."""
        ...


class LinkStore(Protocol):
    """Persistence port for :class:`ShareLink` rows (digest-indexed)."""

    async def add(self, link: ShareLink) -> None:
        """Persist a freshly minted link row (digest only)."""
        ...

    async def get_by_digest(self, tenant_id: UUID, digest: str) -> ShareLink | None:
        """Return the link row for a token digest, or ``None``."""
        ...

    async def save(self, link: ShareLink) -> None:
        """Persist use-count/revocation transitions on a link row."""
        ...


class InMemoryBundleStore:
    """Process-local bundles + grants (synthetic tenants; SQL later).

    Tenant isolation is enforced in Python on every method: a
    cross-tenant id yields ``None`` / empty grants — never a leak.
    """

    def __init__(self) -> None:
        self._bundles: dict[UUID, DocBundle] = {}
        self._grants: dict[UUID, dict[UUID, BundleRole]] = {}

    async def add(self, bundle: DocBundle) -> None:
        """Insert one bundle row."""
        self._bundles[bundle.id] = bundle
        self._grants.setdefault(bundle.id, {})

    async def get(self, tenant_id: UUID, bundle_id: UUID) -> DocBundle | None:
        """Return one bundle iff it belongs to ``tenant_id``."""
        bundle = self._bundles.get(bundle_id)
        if bundle is None or bundle.tenant_id != tenant_id:
            return None
        return bundle

    async def save(self, bundle: DocBundle) -> None:
        """Overwrite the stored bundle row (same id)."""
        self._bundles[bundle.id] = bundle

    async def get_grants(self, tenant_id: UUID, bundle_id: UUID) -> Mapping[UUID, BundleRole]:
        """Return the live grant map (empty for unknown/cross-tenant)."""
        bundle = self._bundles.get(bundle_id)
        if bundle is None or bundle.tenant_id != tenant_id:
            return {}
        return dict(self._grants.get(bundle_id, {}))

    async def save_grant(
        self, tenant_id: UUID, bundle_id: UUID, actor_id: UUID, role: BundleRole
    ) -> None:
        """Upsert one grant (tenant-checked)."""
        from app.domain.errors import NotFound  # noqa: PLC0415

        bundle = self._bundles.get(bundle_id)
        if bundle is None or bundle.tenant_id != tenant_id:
            raise NotFound(f"bundle {bundle_id} not found")
        self._grants.setdefault(bundle_id, {})[actor_id] = role

    def clear(self) -> None:
        """Drop all rows (test isolation only)."""
        self._bundles.clear()
        self._grants.clear()


class InMemoryLinkStore:
    """Process-local share links, indexed by token digest.

    The raw token is never stored — only ``token_sha256`` — so a store
    dump cannot be replayed as bearer credentials.
    """

    def __init__(self) -> None:
        self._by_digest: dict[str, ShareLink] = {}

    async def add(self, link: ShareLink) -> None:
        """Insert one link row keyed by its digest."""
        self._by_digest[link.token_sha256] = link

    async def get_by_digest(self, tenant_id: UUID, digest: str) -> ShareLink | None:
        """Return the link row iff it belongs to ``tenant_id``."""
        link = self._by_digest.get(digest)
        if link is None or link.tenant_id != tenant_id:
            return None
        return link

    async def save(self, link: ShareLink) -> None:
        """Overwrite the stored link row (same digest)."""
        self._by_digest[link.token_sha256] = link

    async def scan_by_digest(self, raw_token: str) -> ShareLink | None:
        """Return the link row for a raw bearer token (any tenant).

        Bearer redemption is deliberately unauthenticated — the token
        *is* the credential — so there is no actor tenant to scope by.
        The tenant is read off the returned row. Unknown or
        too-short tokens yield ``None`` (the caller raises the
        identical UNKNOWN denial either way).
        """
        try:
            digest = hash_token(raw_token)
        except Exception:
            return None
        return self._by_digest.get(digest)

    def clear(self) -> None:
        """Drop all rows (test isolation only)."""
        self._by_digest.clear()


async def _resolve_bundle(
    *, tenant_id: UUID, bundle_id: UUID, bundle_store: BundleStore
) -> DocBundle:
    """Resolve a bundle in-tenant or raise ``NotFound`` (no leak)."""
    from app.domain.errors import NotFound  # noqa: PLC0415

    bundle = await bundle_store.get(tenant_id, bundle_id)
    if bundle is None:
        raise NotFound(f"bundle {bundle_id} not found")
    return bundle


@dataclass(frozen=True, slots=True)
class CreateBundleCommand:
    """Input to :func:`create_bundle`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    name: str
    member_document_ids: tuple[UUID, ...]


async def create_bundle(
    cmd: CreateBundleCommand,
    *,
    document_repo: DocumentRepository,
    bundle_store: BundleStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DocBundle:
    """Create a permissioned bundle over tenant-scoped documents.

    Every member is resolved in-tenant and requires ``DOCUMENT_READ``;
    the bundle grants read access *through the bundle only* and never
    widen the underlying document grants. Restricted members require
    ``DOCUMENT_READ_RESTRICTED`` (fail-closed).
    """
    from app.domain.errors import Forbidden  # noqa: PLC0415

    now = clock.now()
    first_case_id: UUID | None = None
    for document_id in cmd.member_document_ids:
        await authorize(
            Action.DOCUMENT_READ,
            proxy(
                tenant_id=cmd.tenant_id,
                actor_id=cmd.actor_id,
                actor_roles=cmd.actor_roles,
            ),
            Resource(tenant_id=cmd.tenant_id, kind="document", id=document_id),
        )
        document = await resolve_document(
            tenant_id=cmd.tenant_id,
            document_id=document_id,
            document_repo=document_repo,
        )
        if first_case_id is None:
            first_case_id = document.case_id
        if str(document.sensitivity.value) in {"restricted", "phi"} and not await has_action(
            Action.DOCUMENT_READ_RESTRICTED,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
            resource_id=document_id,
        ):
            raise Forbidden(
                "restricted bundle member requires document.read_restricted",
                details={"reason": "restricted_member"},
            )
    bundle = DocBundle.create(
        tenant_id=cmd.tenant_id,
        name=cmd.name,
        created_by=cmd.actor_id,
        member_document_ids=cmd.member_document_ids,
        now=now,
    )
    await bundle_store.add(bundle)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOC_BUNDLE_CREATED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=bundle.id,
        case_id=first_case_id,
        payload={"bundle_id": str(bundle.id), "name": bundle.name},
        now=now,
    )
    return bundle


async def grant_bundle_access(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    bundle_id: UUID,
    grantee_id: UUID,
    role: BundleRole,
    bundle_store: BundleStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> None:
    """Grant ``role`` on a bundle (granter must hold EDITOR)."""
    bundle = await _resolve_bundle(
        tenant_id=tenant_id, bundle_id=bundle_id, bundle_store=bundle_store
    )
    grants = await bundle_store.get_grants(tenant_id, bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=tenant_id,
        actor_id=actor_id,
        grants=grants,
        required=BundleRole.EDITOR,
    )
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=bundle_id),
    )
    await bundle_store.save_grant(tenant_id, bundle_id, grantee_id, role)
    now = clock.now()
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=tenant_id,
        event_type=DOC_BUNDLE_MEMBERS_CHANGED,
        actor_id=actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=bundle_id,
        case_id=None,
        payload={"bundle_id": str(bundle_id), "granted_role": role.value},
        now=now,
    )


async def change_bundle_members(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    bundle_id: UUID,
    added: tuple[UUID, ...],
    removed: tuple[UUID, ...],
    document_repo: DocumentRepository,
    bundle_store: BundleStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DocBundle:
    """Add/remove bundle members (actor must hold EDITOR).

    Added documents resolve in-tenant and require ``DOCUMENT_READ``;
    removing a member (or revoking the bundle) removes the
    bundle-mediated read path immediately.
    """
    bundle = await _resolve_bundle(
        tenant_id=tenant_id, bundle_id=bundle_id, bundle_store=bundle_store
    )
    grants = await bundle_store.get_grants(tenant_id, bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=tenant_id,
        actor_id=actor_id,
        grants=grants,
        required=BundleRole.EDITOR,
    )
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=bundle_id),
    )
    for document_id in added:
        await authorize(
            Action.DOCUMENT_READ,
            proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
            Resource(tenant_id=tenant_id, kind="document", id=document_id),
        )
        await resolve_document(
            tenant_id=tenant_id, document_id=document_id, document_repo=document_repo
        )
    updated = bundle.with_members_added(added).with_members_removed(removed)
    await bundle_store.save(updated)
    now = clock.now()
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=tenant_id,
        event_type=DOC_BUNDLE_MEMBERS_CHANGED,
        actor_id=actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=bundle_id,
        case_id=None,
        payload={
            "bundle_id": str(bundle_id),
            "added": [str(doc_id) for doc_id in added],
            "removed": [str(doc_id) for doc_id in removed],
        },
        now=now,
    )
    return updated


async def revoke_bundle(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    bundle_id: UUID,
    reason: str,
    bundle_store: BundleStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DocBundle:
    """Revoke a bundle (irreversible; kills grants + links implicitly)."""
    bundle = await _resolve_bundle(
        tenant_id=tenant_id, bundle_id=bundle_id, bundle_store=bundle_store
    )
    grants = await bundle_store.get_grants(tenant_id, bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=tenant_id,
        actor_id=actor_id,
        grants=grants,
        required=BundleRole.EDITOR,
    )
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=bundle_id),
    )
    now = clock.now()
    revoked = bundle.revoke(revoked_by=actor_id, reason=reason, now=now)
    await bundle_store.save(revoked)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=tenant_id,
        event_type=DOC_BUNDLE_REVOKED,
        actor_id=actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=bundle_id,
        case_id=None,
        payload={"bundle_id": str(bundle_id)},
        now=now,
    )
    return revoked


@dataclass(frozen=True, slots=True)
class MintLinkCommand:
    """Input to :func:`mint_share_link`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    bundle_id: UUID
    ttl: timedelta
    max_uses: int


@dataclass(frozen=True, slots=True)
class MintedLink:
    """A minted link: the aggregate plus the one-time raw token.

    The raw token exists only here (creation response); it is never
    logged, audited, or persisted — only its digest is stored.
    """

    link: ShareLink
    raw_token: str


async def mint_share_link(
    cmd: MintLinkCommand,
    *,
    bundle_store: BundleStore,
    link_store: LinkStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> MintedLink:
    """Mint a bearer link for a bundle (minter must hold VIEWER).

    Entropy comes from ``secrets.token_urlsafe`` here (application
    layer owns I/O-adjacent randomness); only the SHA-256 digest is
    stored. The audit row records the link id + expiry — never the
    token.
    """
    bundle = await _resolve_bundle(
        tenant_id=cmd.tenant_id, bundle_id=cmd.bundle_id, bundle_store=bundle_store
    )
    grants = await bundle_store.get_grants(cmd.tenant_id, cmd.bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        grants=grants,
        required=BundleRole.VIEWER,
    )
    await authorize(
        Action.DOCUMENT_READ,
        proxy(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
        ),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.bundle_id),
    )
    now = clock.now()
    # Why 24 bytes: ``token_urlsafe(24)`` yields 32 chars, comfortably
    # above the domain's 16-char floor with 192 bits of entropy.
    raw_token = secrets.token_urlsafe(24)
    link = ShareLink.create(
        tenant_id=cmd.tenant_id,
        bundle_id=cmd.bundle_id,
        raw_token=raw_token,
        created_by=cmd.actor_id,
        ttl=cmd.ttl,
        max_uses=cmd.max_uses,
        now=now,
    )
    await link_store.add(link)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=SHARE_LINK_MINTED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=cmd.bundle_id,
        case_id=None,
        payload={
            "bundle_id": str(cmd.bundle_id),
            "link_id": str(link.id),
            "expires_at": link.expires_at.isoformat(),
            "max_uses": link.max_uses,
        },
        now=now,
    )
    return MintedLink(link=link, raw_token=raw_token)


@dataclass(frozen=True, slots=True)
class RedeemedLink:
    """A successful bearer presentation: bundle + member ids."""

    bundle: DocBundle
    member_document_ids: tuple[UUID, ...]


async def redeem_share_link(
    *,
    tenant_id: UUID,
    raw_token: str,
    bundle_store: BundleStore,
    link_store: LinkStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> RedeemedLink:
    """Redeem a bearer token for its bundle's member list.

    Bundle liveness is checked before link liveness so a revoked
    bundle kills its links implicitly. Every denial raises
    ``Forbidden`` with a bare reason code only (no bundle name,
    member list, or existence signal). A successful presentation
    increments the use count and audits ``ShareLinkRedeemed``.
    """
    from app.domain.errors import Forbidden  # noqa: PLC0415

    now = clock.now()
    # Why hash-then-lookup: the raw token is never stored; an
    # unhashable (too-short) token simply misses the index and takes
    # the identical UNKNOWN path as a well-formed stranger.
    try:
        digest = hash_token(raw_token)
    except Exception:
        raise check_link_unknown() from None
    link = await link_store.get_by_digest(tenant_id, digest)
    if link is None:
        raise check_link_unknown()
    bundle = await bundle_store.get(tenant_id, link.bundle_id)
    if bundle is None or bundle.is_revoked:
        raise Forbidden(
            "share link denied",
            details={"reason": ShareLinkDenialReason.REVOKED.value},
        )
    used = link.record_use(now=now)
    await link_store.save(used)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=tenant_id,
        event_type=SHARE_LINK_REDEEMED,
        actor_id=link.created_by,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=bundle.id,
        case_id=None,
        payload={"bundle_id": str(bundle.id), "link_id": str(link.id)},
        now=now,
    )
    return RedeemedLink(bundle=bundle, member_document_ids=bundle.member_document_ids)


async def revoke_share_link(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    raw_token: str,
    reason: str,
    bundle_store: BundleStore,
    link_store: LinkStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> ShareLink:
    """Revoke a link by its token (revoker must hold bundle VIEWER)."""
    try:
        digest = hash_token(raw_token)
    except Exception:
        raise check_link_unknown() from None
    link = await link_store.get_by_digest(tenant_id, digest)
    if link is None:
        raise check_link_unknown()
    bundle = await _resolve_bundle(
        tenant_id=tenant_id, bundle_id=link.bundle_id, bundle_store=bundle_store
    )
    grants = await bundle_store.get_grants(tenant_id, link.bundle_id)
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=tenant_id,
        actor_id=actor_id,
        grants=grants,
        required=BundleRole.VIEWER,
    )
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=link.bundle_id),
    )
    now = clock.now()
    revoked = link.revoke(revoked_by=actor_id, reason=reason, now=now)
    await link_store.save(revoked)
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=tenant_id,
        event_type=SHARE_LINK_REVOKED,
        actor_id=actor_id,
        subject_type=SUBJECT_TYPE_BUNDLE,
        subject_id=link.bundle_id,
        case_id=None,
        payload={"bundle_id": str(link.bundle_id), "link_id": str(link.id)},
        now=now,
    )
    return revoked
