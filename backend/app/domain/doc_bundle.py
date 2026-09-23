"""Permissioned document bundles (envelope equivalent) + share-by-link (T5).

This module is the **pure-domain** kernel for the T5 collaboration rule
from ``specs/002-leftovers/spec.md`` §3.T5 ("permissioned bundles ...
with independent sharing, not searchable by content" and
"share-by-link/file with expiry + auth check") and user story T5-US3
("bundle/share documents with independent permissions and expiry"):

1. **DocBundle** — a user-created set of documents with *independent*
   sharing. "Independent" means bundle grants confer read access to the
   member documents *through the bundle only*; they never widen the
   underlying document grants, and removing a document from the bundle
   (or revoking the bundle) removes that path immediately. Bundles are
   **never a content-search scope**: membership must not make a
   document's bytes searchable to someone who could not already read
   the document directly. That exclusion is enforced where search
   executes (the search application layer never takes a bundle as a
   corpus); this module documents the contract and keeps the only
   bundle read path — ``check_bundle_access`` — explicit.
2. **ShareLink** — a bearer token bound to one bundle with a bounded
   lifetime (``MIN_LINK_TTL``..``MAX_LINK_TTL``) and a bounded number
   of uses. Only the SHA-256 of the token is stored
   (``token_sha256``); the raw token exists only in the creation
   response and is never logged, audited, or persisted. Every denial
   (expired, revoked, exhausted, unknown) raises ``Forbidden`` whose
   details carry *only* a safe reason code — no bundle name, member
   list, or existence signal beyond the bare denial — so a guessed-URL
   probe learns nothing (T5 acceptance: "expired link denies with no
   metadata leak").

# Why the domain owns the token hash, not the token

Hashing is deterministic string processing (``hashlib.sha256``), not
I/O, so it belongs here: the application layer generates entropy
(``secrets.token_urlsafe``), hashes it through ``hash_token``, and
stores the digest. Verification re-hashes the presented token and
compares digests with ``hmac.compare_digest`` (constant-time) so the
check does not leak prefix information through timing.

# INTEGRATION_SEAM: outbound share channels

Desktop-bridge payloads and mail/file shares (T8 named channels) reuse
``ShareLink`` as the expiry + auth-check kernel: the channel adapter
delivers the raw token out-of-band while this module (via the
application use case) enforces lifetime, use-count, and revocation.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from app.domain.errors import Forbidden, ValidationFailed

# Minimum share-link lifetime. A zero-second link would be useless and
# almost certainly a client-side bug; refuse to mint one so the audit
# row still shows a meaningful "expires at" timestamp (mirrors the
# break-glass ``MIN_GRANT_DURATION`` rationale).
MIN_LINK_TTL: Final[timedelta] = timedelta(minutes=5)

# Maximum share-link lifetime. Sharing is a convenience path, not a
# standing grant: 30 days bounds the exposure window of a bearer token
# that may live in mailboxes and browser histories. Longer
# collaboration uses bundle grants (revocable per-user) instead.
MAX_LINK_TTL: Final[timedelta] = timedelta(days=30)

# Minimum raw-token length accepted for hashing. ``secrets.token_urlsafe(24)``
# yields 32 chars; 16 is a conservative floor that rejects empty /
# truncated tokens without coupling the domain to one generator.
MIN_TOKEN_CHARS = 16

# Maximum documents per bundle. An envelope is a curated set, not a
# bulk export (bulk export is the T8 evidence-bundle path with its own
# policy version + actor + timestamp audit). The cap keeps the
# membership list — which the audit row reproduces — bounded.
MAX_BUNDLE_MEMBERS = 500

_MAX_BUNDLE_NAME_CHARS = 200
_MAX_REASON_CHARS = 500

# Safe, metadata-free reason codes for ``Forbidden.details`` on bundle
# denials. (Link denials use ``ShareLinkDenialReason`` values directly.)
# The details mapping must never carry bundle names, document ids,
# member counts, or tokens.
_REASON_CROSS_TENANT = "cross_tenant"
_REASON_REVOKED = "revoked"
_REASON_NOT_GRANTED = "bundle_access_not_granted"


class BundleRole(StrEnum):
    """The per-actor grants a bundle may carry.

    Roles form a hierarchy: ``EDITOR`` implies ``VIEWER``. There is no
    "owner" grant value — the bundle's ``created_by`` actor always holds
    editor-equivalent access implicitly, so ownership survives even when
    the grant map is empty.
    """

    VIEWER = "viewer"
    EDITOR = "editor"


class ShareLinkDenialReason(StrEnum):
    """Structured reason for a denied share-link access.

    Mirrors the ``BreakGlassDenialReason`` shape so the HTTP layer maps
    reasons to status codes uniformly and the audit row carries the same
    explanation the presenter would have seen.
    """

    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    EXHAUSTED = "exhausted"


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest stored for a share-link token.

    Args:
        raw_token: The bearer token as generated (``secrets``) and
            delivered out-of-band. Validated for minimum length only —
            entropy is the generator's job.

    Raises:
        ValidationFailed: With ``field`` set to ``token`` when the
            token is empty or shorter than ``MIN_TOKEN_CHARS``.
    """
    if not isinstance(raw_token, str) or len(raw_token) < MIN_TOKEN_CHARS:
        raise ValidationFailed(
            f"share token must be at least {MIN_TOKEN_CHARS} characters",
            field="token",
        )
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def verify_token(*, presented: str, expected_sha256: str) -> bool:
    """Constant-time comparison of a presented token against a digest.

    The presented value is hashed first so the raw token never needs to
    be stored; ``hmac.compare_digest`` avoids leaking prefix matches
    through timing. Returns False (never raises) on any mismatch,
    including a malformed presented value — unknown-token handling is
    the caller's ``UNKNOWN`` path, not an exception here.
    """
    if not isinstance(presented, str) or not presented:
        return False
    try:
        candidate = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected_sha256)


@dataclass(frozen=True, slots=True)
class DocBundle:
    """A permissioned document set with independent sharing (T5-US3).

    Membership is a tuple of document ids (deduplicated, order
    preserved). The bundle stores *references*, not copies: removing a
    member (or revoking the bundle) removes the bundle-mediated read
    path immediately, while the underlying documents and their grants
    are untouched.
    """

    id: UUID
    tenant_id: UUID
    name: str
    created_by: UUID
    member_document_ids: tuple[UUID, ...] = ()
    status: str = "ACTIVE"
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None
    revoke_reason: str | None = None

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        name: str,
        created_by: UUID,
        member_document_ids: tuple[UUID, ...] = (),
        now: datetime,
    ) -> DocBundle:
        """Construct a fresh ACTIVE bundle.

        Args:
            tenant_id: The owning tenant (members must belong here —
                enforced by the application use case, which loads the
                documents; the domain pins the authority here).
            name: Operator label, 1..200 chars after stripping. The
                name is metadata shown to granted actors only — it never
                appears in link-denial details.
            created_by: The owner; holds implicit editor access.
            member_document_ids: Initial members, deduplicated with
                order preserved; at most ``MAX_BUNDLE_MEMBERS``.
            now: The application clock timestamp (UTC).
        """
        stripped = name.strip() if isinstance(name, str) else ""
        if not stripped or len(stripped) > _MAX_BUNDLE_NAME_CHARS:
            raise ValidationFailed(
                "bundle name is required (1..200 chars)",
                field="name",
            )
        deduped = tuple(dict.fromkeys(member_document_ids))
        if len(deduped) > MAX_BUNDLE_MEMBERS:
            raise ValidationFailed(
                f"bundle supports at most {MAX_BUNDLE_MEMBERS} members, got {len(deduped)}",
                field="member_document_ids",
            )
        return DocBundle(
            id=uuid4(),
            tenant_id=tenant_id,
            name=stripped,
            created_by=created_by,
            member_document_ids=deduped,
            created_at=now,
        )

    @property
    def is_revoked(self) -> bool:
        """True once ``revoke`` has been applied (irreversible)."""
        return self.status == "REVOKED"

    def with_members_added(self, member_ids: tuple[UUID, ...]) -> DocBundle:
        """Return a copy with ``member_ids`` appended (deduplicated).

        Raises:
            ValidationFailed: When the bundle is revoked (a revoked
                bundle is frozen — create a new one) or the cap would
                be exceeded.
        """
        if self.is_revoked:
            raise ValidationFailed(
                "revoked bundles are frozen; create a new bundle instead",
                field="member_document_ids",
            )
        merged = tuple(dict.fromkeys((*self.member_document_ids, *member_ids)))
        if len(merged) > MAX_BUNDLE_MEMBERS:
            raise ValidationFailed(
                f"bundle supports at most {MAX_BUNDLE_MEMBERS} members, got {len(merged)}",
                field="member_document_ids",
            )
        # Why a new dataclass, not mutation: frozen aggregates keep the
        # audit row ("members added: [...]") provable against the prior
        # state the caller still holds.
        return DocBundle(
            id=self.id,
            tenant_id=self.tenant_id,
            name=self.name,
            created_by=self.created_by,
            member_document_ids=merged,
            status=self.status,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
            revoked_by=self.revoked_by,
            revoke_reason=self.revoke_reason,
        )

    def with_members_removed(self, member_ids: tuple[UUID, ...]) -> DocBundle:
        """Return a copy without ``member_ids`` (absent ids ignored).

        Removing the last member is allowed (an empty bundle simply
        grants nothing); deleting the bundle itself is a separate
        lifecycle the retention track owns.
        """
        if self.is_revoked:
            raise ValidationFailed(
                "revoked bundles are frozen; create a new bundle instead",
                field="member_document_ids",
            )
        doomed = set(member_ids)
        return DocBundle(
            id=self.id,
            tenant_id=self.tenant_id,
            name=self.name,
            created_by=self.created_by,
            member_document_ids=tuple(m for m in self.member_document_ids if m not in doomed),
            status=self.status,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
            revoked_by=self.revoked_by,
            revoke_reason=self.revoke_reason,
        )

    def revoke(self, *, revoked_by: UUID, reason: str, now: datetime) -> DocBundle:
        """Return a revoked copy (irreversible; revoking twice is a no-op).

        Revocation freezes membership changes and denies every read
        path (bundle grants and all links bound to the bundle — the
        application use case checks bundle liveness before link
        liveness so a revoked bundle kills its links implicitly).
        """
        if self.is_revoked:
            return self
        stripped = reason.strip() if isinstance(reason, str) else ""
        if not stripped or len(stripped) > _MAX_REASON_CHARS:
            raise ValidationFailed(
                "revoke reason is required (1..500 chars)",
                field="reason",
            )
        return DocBundle(
            id=self.id,
            tenant_id=self.tenant_id,
            name=self.name,
            created_by=self.created_by,
            member_document_ids=self.member_document_ids,
            status="REVOKED",
            created_at=self.created_at,
            revoked_at=now,
            revoked_by=revoked_by,
            revoke_reason=stripped,
        )


def _role_covers(*, held: BundleRole, required: BundleRole) -> bool:
    """True when ``held`` satisfies ``required`` under the hierarchy."""
    if required is BundleRole.VIEWER:
        return True
    return held is BundleRole.EDITOR


def check_bundle_access(
    *,
    bundle: DocBundle,
    actor_tenant_id: UUID,
    actor_id: UUID,
    grants: Mapping[UUID, BundleRole],
    required: BundleRole,
) -> None:
    """Enforce the bundle read path for one actor.

    Args:
        bundle: The bundle being read.
        actor_tenant_id: The tenant the actor is acting in.
        actor_id: The actor requesting access.
        grants: The bundle's live per-actor grant map (resolved by the
            application layer from the grant store).
        required: The minimum role for the attempted action (``VIEWER``
            to read members, ``EDITOR`` to change membership).

    Returns:
        None on success (the bundle-mediated read may proceed to the
        per-document sensitivity check — bundle access never bypasses
        ``document_protection.check_read_allowed`` for restricted
        originals).

    Raises:
        Forbidden: On cross-tenant access (checked first, even with a
            grant), on a revoked bundle, or when neither ownership nor
            a covering grant authorizes the action. ``details`` carries
            only a safe ``reason`` code — never the bundle name, member
            list, or grant map.
    """
    if actor_tenant_id != bundle.tenant_id:
        raise Forbidden(
            "cross-tenant bundle access denied",
            details={"reason": _REASON_CROSS_TENANT},
        )
    if bundle.is_revoked:
        raise Forbidden(
            "bundle is revoked",
            details={"reason": _REASON_REVOKED},
        )
    if actor_id == bundle.created_by:
        # Why the owner bypasses the grant map: ownership is the
        # standing editor grant, so an empty map never locks the owner
        # out of their own bundle.
        return
    held = grants.get(actor_id)
    if held is None or not _role_covers(held=held, required=required):
        raise Forbidden(
            "bundle access denied",
            details={"reason": _REASON_NOT_GRANTED},
        )


@dataclass(frozen=True, slots=True)
class ShareLink:
    """A bearer token bound to one bundle with bounded life + uses (T5-US3).

    The stored form carries only ``token_sha256`` (see ``hash_token``).
    Presentation (token lookup + ``verify_token``) lives in the
    application use case against the link store; this aggregate owns
    the lifetime rules and the use-count transition.
    """

    id: UUID
    tenant_id: UUID
    bundle_id: UUID
    token_sha256: str
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    max_uses: int
    use_count: int = 0
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None
    revoke_reason: str | None = field(default=None)

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        bundle_id: UUID,
        raw_token: str,
        created_by: UUID,
        ttl: timedelta,
        max_uses: int,
        now: datetime,
    ) -> ShareLink:
        """Mint a fresh, live link for ``bundle_id``.

        Args:
            tenant_id: Must equal the bundle's tenant (checked by the
                application use case, which loads the bundle).
            bundle_id: The bundle this link opens.
            raw_token: The generated bearer token; only its digest is
                stored (validated via ``hash_token``).
            created_by: The actor minting the link (must hold at least
                bundle VIEWER — enforced by the application use case via
                ``check_bundle_access`` before minting).
            ttl: Lifetime from ``now``; clamped to
                ``MIN_LINK_TTL``..``MAX_LINK_TTL`` (rejected outside).
            max_uses: Total presentations allowed (>= 1).
            now: The application clock timestamp (UTC).
        """
        digest = hash_token(raw_token)
        if not isinstance(ttl, timedelta) or ttl < MIN_LINK_TTL:
            min_seconds = int(MIN_LINK_TTL.total_seconds())
            raise ValidationFailed(
                f"link ttl must be at least {min_seconds}s",
                field="ttl",
            )
        if ttl > MAX_LINK_TTL:
            max_days = int(MAX_LINK_TTL.total_seconds() // 86400)
            raise ValidationFailed(
                f"link ttl must be at most {max_days} days",
                field="ttl",
            )
        if max_uses < 1:
            raise ValidationFailed("max_uses must be >= 1", field="max_uses")
        return ShareLink(
            id=uuid4(),
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            token_sha256=digest,
            created_by=created_by,
            created_at=now,
            expires_at=now + ttl,
            max_uses=max_uses,
        )

    def check(self, *, now: datetime) -> ShareLinkDenialReason | None:
        """Return ``None`` when the link is live at ``now``.

        Returns a :class:`ShareLinkDenialReason` when the link does not
        authorize presentation so the caller can emit a structured audit
        row and HTTP 403 with the same reason. Order matters: a revoked
        link reports ``REVOKED`` even after expiry so the timeline reads
        correctly (mirrors ``BreakGlassGrant.check`` ordering).
        """
        if self.revoked_at is not None:
            return ShareLinkDenialReason.REVOKED
        if now >= self.expires_at:
            return ShareLinkDenialReason.EXPIRED
        if self.use_count >= self.max_uses:
            return ShareLinkDenialReason.EXHAUSTED
        return None

    def is_live(self, *, now: datetime) -> bool:
        """True iff ``check`` reports no denial at ``now``."""
        return self.check(now=now) is None

    def record_use(self, *, now: datetime) -> ShareLink:
        """Return a copy with ``use_count`` incremented.

        Raises:
            Forbidden: When the link is not live (expired / revoked /
                exhausted). ``details`` carries only the reason code —
                the presenter learns nothing about the bundle behind
                the link.
        """
        denial = self.check(now=now)
        if denial is not None:
            raise Forbidden(
                "share link denied",
                details={"reason": denial.value},
            )
        return ShareLink(
            id=self.id,
            tenant_id=self.tenant_id,
            bundle_id=self.bundle_id,
            token_sha256=self.token_sha256,
            created_by=self.created_by,
            created_at=self.created_at,
            expires_at=self.expires_at,
            max_uses=self.max_uses,
            use_count=self.use_count + 1,
            revoked_at=self.revoked_at,
            revoked_by=self.revoked_by,
            revoke_reason=self.revoke_reason,
        )

    def revoke(self, *, revoked_by: UUID, reason: str, now: datetime) -> ShareLink:
        """Return a revoked copy (irreversible; revoking twice is a no-op)."""
        if self.revoked_at is not None:
            return self
        stripped = reason.strip() if isinstance(reason, str) else ""
        if not stripped or len(stripped) > _MAX_REASON_CHARS:
            raise ValidationFailed(
                "revoke reason is required (1..500 chars)",
                field="reason",
            )
        return ShareLink(
            id=self.id,
            tenant_id=self.tenant_id,
            bundle_id=self.bundle_id,
            token_sha256=self.token_sha256,
            created_by=self.created_by,
            created_at=self.created_at,
            expires_at=self.expires_at,
            max_uses=self.max_uses,
            use_count=self.use_count,
            revoked_at=now,
            revoked_by=revoked_by,
            revoke_reason=stripped,
        )


def check_link_unknown() -> Forbidden:
    """Build the denial for a token that matches no stored link.

    Unknown tokens deny with ``link_unknown`` and no other signal — the
    response must be indistinguishable (modulo the reason code) from an
    expired link so a prober cannot enumerate live links by comparing
    responses. Factored as a constructor (not raised inline) so the
    application use case raises the identical shape from its lookup
    miss path.
    """
    return Forbidden(
        "share link denied",
        details={"reason": ShareLinkDenialReason.UNKNOWN.value},
    )


__all__ = [
    "MAX_BUNDLE_MEMBERS",
    "MAX_LINK_TTL",
    "MIN_LINK_TTL",
    "MIN_TOKEN_CHARS",
    "BundleRole",
    "DocBundle",
    "ShareLink",
    "ShareLinkDenialReason",
    "check_bundle_access",
    "check_link_unknown",
    "hash_token",
    "verify_token",
]
