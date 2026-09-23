"""Pure-domain tests for permissioned bundles + share-by-link (T5 / 002-leftovers).

Covers the rules in ``app.domain.doc_bundle``:

- bundle permission model: owner bypass, viewer/editor hierarchy,
  revoked-bundle and cross-tenant denials with metadata-free details;
- membership lifecycle: dedup, cap, frozen-on-revoke, removal;
- link-expiry rules: bounded TTL, live-then-expired transition, use
  counting, exhaustion, revocation, and the no-metadata-leak denial
  shape (expired/unknown links deny with a bare reason code).

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Structure is ``// given`` / ``// when`` / ``// then``. Deterministic: fixed
clock timestamp, fixed UUIDs, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.domain.doc_bundle import (
    BundleRole,
    DocBundle,
    ShareLink,
    ShareLinkDenialReason,
    check_bundle_access,
    check_link_unknown,
    hash_token,
    verify_token,
)
from app.domain.errors import Forbidden, ValidationFailed

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
OWNER_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
VIEWER_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
STRANGER_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
DOC_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
RAW_TOKEN = "0123456789abcdef-token-seed-42"

_GRANTS_VIEWER: dict[UUID, BundleRole] = {VIEWER_ID: BundleRole.VIEWER}


def _bundle() -> DocBundle:
    """Build a two-member bundle owned by ``OWNER_ID``."""
    return DocBundle.create(
        tenant_id=ACME_TENANT,
        name="Hearing exhibits",
        created_by=OWNER_ID,
        member_document_ids=(DOC_A, DOC_B),
        now=FIXED_NOW,
    )


def _link(
    *,
    ttl: timedelta = timedelta(hours=1),
    max_uses: int = 3,
    now: datetime = FIXED_NOW,
) -> ShareLink:
    """Mint a live link for a throwaway bundle id."""
    return ShareLink.create(
        tenant_id=ACME_TENANT,
        bundle_id=_bundle().id,
        raw_token=RAW_TOKEN,
        created_by=OWNER_ID,
        ttl=ttl,
        max_uses=max_uses,
        now=now,
    )


# ---------------------------------------------------------------------------
# Bundle creation + membership
# ---------------------------------------------------------------------------


def test_bundle_create__duplicate_members__deduplicates() -> None:
    """Membership is a set: repeats collapse, order is preserved."""
    # // given duplicate initial members
    # // when creating // then the bundle holds each id once.
    bundle = DocBundle.create(
        tenant_id=ACME_TENANT,
        name="Dupes",
        created_by=OWNER_ID,
        member_document_ids=(DOC_A, DOC_B, DOC_A),
        now=FIXED_NOW,
    )
    assert bundle.member_document_ids == (DOC_A, DOC_B)


def test_bundle_create__blank_name__rejects() -> None:
    """A bundle needs an operator-visible label."""
    # // given a blank name
    # // when creating // then ValidationFailed names name.
    with pytest.raises(ValidationFailed, match="bundle name is required"):
        DocBundle.create(
            tenant_id=ACME_TENANT,
            name="   ",
            created_by=OWNER_ID,
            now=FIXED_NOW,
        )


def test_bundle_members__add_and_remove__round_trips() -> None:
    """Membership edits return new aggregates; removal of absent ids is safe."""
    # // given a bundle
    bundle = _bundle()
    # // when adding a repeat + removing one member and an absent id
    grown = bundle.with_members_added((DOC_B,))
    shrunk = grown.with_members_removed((DOC_A, UUID(int=0)))
    # // then membership reflects exactly the surviving member.
    assert grown.member_document_ids == (DOC_A, DOC_B)
    assert shrunk.member_document_ids == (DOC_B,)
    assert bundle.member_document_ids == (DOC_A, DOC_B)


def test_bundle_members__edit_after_revoke__rejects_as_frozen() -> None:
    """A revoked bundle is frozen; sharing restarts from a new bundle."""
    # // given a revoked bundle
    revoked = _bundle().revoke(revoked_by=OWNER_ID, reason="matter closed", now=FIXED_NOW)
    # // when editing membership // then ValidationFailed is raised.
    with pytest.raises(ValidationFailed, match="frozen"):
        revoked.with_members_added((DOC_A,))


# ---------------------------------------------------------------------------
# Bundle permission model
# ---------------------------------------------------------------------------


def test_bundle_access__owner_without_grant__allows() -> None:
    """Ownership is the standing editor grant, even with an empty map."""
    # // given a bundle and an empty grant map
    bundle = _bundle()
    # // when the owner reads // then no error is raised.
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=ACME_TENANT,
        actor_id=OWNER_ID,
        grants={},
        required=BundleRole.EDITOR,
    )


def test_bundle_access__granted_viewer__allows_read() -> None:
    """A viewer grant opens the bundle-mediated read path."""
    # // given a viewer grant
    bundle = _bundle()
    # // when the viewer reads // then no error is raised.
    check_bundle_access(
        bundle=bundle,
        actor_tenant_id=ACME_TENANT,
        actor_id=VIEWER_ID,
        grants=dict(_GRANTS_VIEWER),
        required=BundleRole.VIEWER,
    )


def test_bundle_access__viewer_attempts_edit__denies() -> None:
    """Viewer does not imply editor under the role hierarchy."""
    # // given a viewer grant
    bundle = _bundle()
    # // when the viewer attempts an edit // then Forbidden is metadata-free.
    with pytest.raises(Forbidden) as exc_info:
        check_bundle_access(
            bundle=bundle,
            actor_tenant_id=ACME_TENANT,
            actor_id=VIEWER_ID,
            grants=dict(_GRANTS_VIEWER),
            required=BundleRole.EDITOR,
        )
    assert exc_info.value.details == {"reason": "bundle_access_not_granted"}


def test_bundle_access__stranger__denies_without_metadata() -> None:
    """An ungranted actor learns nothing about the bundle's contents."""
    # // given no grant for the actor
    bundle = _bundle()
    # // when the stranger reads // then Forbidden carries only the reason.
    with pytest.raises(Forbidden) as exc_info:
        check_bundle_access(
            bundle=bundle,
            actor_tenant_id=ACME_TENANT,
            actor_id=STRANGER_ID,
            grants=dict(_GRANTS_VIEWER),
            required=BundleRole.VIEWER,
        )
    assert exc_info.value.details == {"reason": "bundle_access_not_granted"}
    assert "Hearing exhibits" not in str(exc_info.value.details)


def test_bundle_access__revoked_bundle__denies_even_owner() -> None:
    """Revocation kills the read path for grants and owner alike."""
    # // given a revoked bundle
    revoked = _bundle().revoke(revoked_by=OWNER_ID, reason="matter closed", now=FIXED_NOW)
    # // when the owner reads // then Forbidden reports revocation only.
    with pytest.raises(Forbidden) as exc_info:
        check_bundle_access(
            bundle=revoked,
            actor_tenant_id=ACME_TENANT,
            actor_id=OWNER_ID,
            grants={},
            required=BundleRole.VIEWER,
        )
    assert exc_info.value.details == {"reason": "revoked"}


def test_bundle_access__foreign_tenant__denies_fail_closed() -> None:
    """Cross-tenant bundle access denies even with a grant in hand."""
    # // given a grant that names the actor
    bundle = _bundle()
    grants = {STRANGER_ID: BundleRole.EDITOR}
    # // when the actor presents from another tenant // then cross_tenant.
    with pytest.raises(Forbidden) as exc_info:
        check_bundle_access(
            bundle=bundle,
            actor_tenant_id=OTHER_TENANT,
            actor_id=STRANGER_ID,
            grants=grants,
            required=BundleRole.VIEWER,
        )
    assert exc_info.value.details == {"reason": "cross_tenant"}


# ---------------------------------------------------------------------------
# Token hashing
# ---------------------------------------------------------------------------


def test_token_hash__round_trip__verifies() -> None:
    """The stored digest verifies the presented token in constant time."""
    # // given a digest of the raw token
    digest = hash_token(RAW_TOKEN)
    # // when verifying // then the right token passes, others fail.
    assert digest != RAW_TOKEN
    assert verify_token(presented=RAW_TOKEN, expected_sha256=digest) is True
    assert verify_token(presented="wrong-token-value-0000", expected_sha256=digest) is False
    assert verify_token(presented="", expected_sha256=digest) is False


def test_token_hash__short_token__rejects() -> None:
    """Truncated tokens are refused so a weak bearer cannot be minted."""
    # // given a short token
    # // when hashing // then ValidationFailed names token.
    with pytest.raises(ValidationFailed, match="at least 16 characters"):
        hash_token("tiny")


# ---------------------------------------------------------------------------
# Share-link expiry rules
# ---------------------------------------------------------------------------


def test_link_create__zero_ttl__rejects() -> None:
    """A zero-second link is a client bug; minting is refused."""
    # // given a zero ttl
    # // when creating // then ValidationFailed names ttl.
    with pytest.raises(ValidationFailed, match="must be at least"):
        ShareLink.create(
            tenant_id=ACME_TENANT,
            bundle_id=_bundle().id,
            raw_token=RAW_TOKEN,
            created_by=OWNER_ID,
            ttl=timedelta(0),
            max_uses=1,
            now=FIXED_NOW,
        )


def test_link_create__excessive_ttl__rejects() -> None:
    """Bearer tokens are convenience paths, not standing grants."""
    # // given a 90-day ttl
    # // when creating // then ValidationFailed caps at 30 days.
    with pytest.raises(ValidationFailed, match="at most 30 days"):
        ShareLink.create(
            tenant_id=ACME_TENANT,
            bundle_id=_bundle().id,
            raw_token=RAW_TOKEN,
            created_by=OWNER_ID,
            ttl=timedelta(days=90),
            max_uses=1,
            now=FIXED_NOW,
        )


def test_link_check__within_ttl__is_live() -> None:
    """A fresh link authorizes presentation."""
    # // given a fresh link
    link = _link()
    # // when checking before expiry // then it is live.
    assert link.is_live(now=FIXED_NOW) is True
    assert link.check(now=FIXED_NOW) is None


def test_link_check__past_expiry__denies_expired() -> None:
    """An expired link reports EXPIRED (acceptance: expiry denies)."""
    # // given a link checked after its expiry
    link = _link(ttl=timedelta(minutes=5))
    late = FIXED_NOW + timedelta(minutes=6)
    # // when checking // then the denial reason is EXPIRED.
    assert link.check(now=late) is ShareLinkDenialReason.EXPIRED
    assert link.is_live(now=late) is False


def test_link_record_use__expired_link__denies_with_no_metadata_leak() -> None:
    """An expired presentation denies with a bare reason code only."""
    # // given an expired link
    link = _link(ttl=timedelta(minutes=5))
    late = FIXED_NOW + timedelta(minutes=6)
    # // when recording a presentation // then Forbidden leaks nothing.
    with pytest.raises(Forbidden) as exc_info:
        link.record_use(now=late)
    assert exc_info.value.details == {"reason": "expired"}
    assert str(link.bundle_id) not in str(exc_info.value.details)


def test_link_unknown_token__denies_without_existence_signal() -> None:
    """An unknown token denies indistinguishably from a dead link."""
    # // given no stored link for the token
    # // when building the lookup-miss denial // then only the reason.
    denial = check_link_unknown()
    assert isinstance(denial, Forbidden)
    assert denial.details == {"reason": "unknown"}


def test_link_record_use__up_to_max__then_exhausts() -> None:
    """Use counting caps presentations; the (max+1)-th denies."""
    # // given a single-use link already presented once
    link = _link(max_uses=1).record_use(now=FIXED_NOW)
    # // when presenting again // then the link is exhausted.
    assert link.use_count == 1
    assert link.check(now=FIXED_NOW) is ShareLinkDenialReason.EXHAUSTED
    with pytest.raises(Forbidden) as exc_info:
        link.record_use(now=FIXED_NOW)
    assert exc_info.value.details == {"reason": "exhausted"}


def test_link_revoke__revoked_link__denies_even_before_expiry() -> None:
    """Revocation wins over a still-valid expiry (timeline reads revocation)."""
    # // given a revoked link inside its ttl
    link = _link(ttl=timedelta(days=7)).revoke(
        revoked_by=OWNER_ID, reason="sent to wrong recipient", now=FIXED_NOW
    )
    # // when checking // then REVOKED, and re-revoke is a no-op.
    assert link.check(now=FIXED_NOW) is ShareLinkDenialReason.REVOKED
    assert link.revoke(revoked_by=OWNER_ID, reason="again", now=FIXED_NOW) is link
