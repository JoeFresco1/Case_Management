"""Domain tests for viewer annotations, redaction renditions, checkout locks (T5).

Covers ``app.domain.annotations``: the annotation overlay layer, the
redaction propose/apply/burn flow (original preserved, burn is a new
rendition), the rendition resolver, the persistent checkout lock
(holder + expiry conflict contract), the pure concurrent-edit rule,
and the re-index request value object.

Style: ``test_<unit>_<scenario>__<expectation>`` with
``// given`` / ``// when`` / ``// then`` sections. Pure-domain only —
no I/O, no DB, fixed tenant + fixed now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.annotations import (
    Annotation,
    AnnotationKind,
    CheckoutConflict,
    CheckoutLock,
    EditLockDecision,
    PermissionDenied,
    Redaction,
    RedactionStatus,
    ReindexRequest,
    RenditionKind,
    ViewerMode,
    evaluate_edit_lock,
    resolve_viewer_rendition,
)
from app.domain.errors import InvariantViolation, ValidationFailed

TENANT = UUID("11111111-1111-1111-1111-111111111111")
DOC = UUID("22222222-2222-2222-2222-222222222222")
ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)

GEOMETRY: dict[str, object] = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}


def _lock(*, holder: UUID = ALICE, now: datetime = NOW, lease: int = 3600) -> CheckoutLock:
    """Build a live checkout lock for the fixed document."""
    return CheckoutLock.acquire(
        tenant_id=TENANT,
        document_id=DOC,
        holder_user_id=holder,
        now=now,
        lease_seconds=lease,
    )


# ---------------------------------------------------------------------------
# Annotation factory
# ---------------------------------------------------------------------------


def test_create_annotation_highlight__valid_geometry() -> None:
    """A highlight with bounds + author constructs cleanly."""
    # given:
    now = NOW

    # when:
    ann = Annotation.create(
        tenant_id=TENANT,
        document_id=DOC,
        document_version=2,
        page=3,
        kind=AnnotationKind.HIGHLIGHT,
        geometry=dict(GEOMETRY),
        content=None,
        author_id=ALICE,
        now=now,
    )

    # then:
    assert ann.kind is AnnotationKind.HIGHLIGHT
    assert ann.page == 3
    assert ann.document_version == 2
    assert ann.geometry == GEOMETRY


def test_create_annotation_text_box__missing_content__rejects() -> None:
    """A text_box with no text is uninterpretable, so it is refused."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="text_box.*content"):
        Annotation.create(
            tenant_id=TENANT,
            document_id=DOC,
            document_version=1,
            page=1,
            kind="text_box",
            geometry=dict(GEOMETRY),
            content=None,
            author_id=ALICE,
            now=now,
        )


def test_create_annotation__unknown_kind__rejects() -> None:
    """Kinds outside the viewer vocabulary are refused at construction."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="unknown annotation kind"):
        Annotation.create(
            tenant_id=TENANT,
            document_id=DOC,
            document_version=1,
            page=1,
            kind="laser_pointer",
            geometry=dict(GEOMETRY),
            content=None,
            author_id=ALICE,
            now=now,
        )


def test_create_annotation__bad_page__rejects() -> None:
    """Pages are 1-based; page 0 is refused."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="page must be >= 1"):
        Annotation.create(
            tenant_id=TENANT,
            document_id=DOC,
            document_version=1,
            page=0,
            kind="highlight",
            geometry=dict(GEOMETRY),
            content=None,
            author_id=ALICE,
            now=now,
        )


def test_create_annotation__empty_geometry__rejects() -> None:
    """An annotation with no bounds cannot be rendered, so it is refused."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="geometry is required"):
        Annotation.create(
            tenant_id=TENANT,
            document_id=DOC,
            document_version=1,
            page=1,
            kind="stamp",
            geometry={},
            content="APPROVED",
            author_id=ALICE,
            now=now,
        )


def test_create_annotation__copies_geometry__caller_mutation_safe() -> None:
    """Later caller-side mutation of the input mapping cannot alter the row."""
    # given:
    geometry = dict(GEOMETRY)

    # when:
    ann = Annotation.create(
        tenant_id=TENANT,
        document_id=DOC,
        document_version=1,
        page=1,
        kind="freehand",
        geometry=geometry,
        content=None,
        author_id=ALICE,
        now=NOW,
    )
    geometry["x"] = 0.99

    # then:
    assert ann.geometry["x"] == 0.1


# ---------------------------------------------------------------------------
# Redaction propose / apply / burn
# ---------------------------------------------------------------------------


def test_propose_redaction__valid_mark__proposed() -> None:
    """A proposed mark records page + region + reason in PROPOSED status."""
    # given:
    now = NOW

    # when:
    mark = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=1,
        page=2,
        region="page 2, paragraph 3",
        reason="contains account number",
        author_id=ALICE,
        now=now,
    )

    # then:
    assert mark.status is RedactionStatus.PROPOSED
    assert mark.base_version == 1
    assert mark.applied_at is None


def test_apply_redaction__proposed__applied() -> None:
    """Applying a proposal records the burn-approval instant."""
    # given:
    mark = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=1,
        page=1,
        region="header block",
        reason="contains account number",
        author_id=ALICE,
        now=NOW,
    )

    # when:
    applied = mark.apply(now=NOW + timedelta(minutes=5))

    # then:
    assert applied.status is RedactionStatus.APPLIED
    assert applied.applied_at == NOW + timedelta(minutes=5)


def test_apply_redaction__already_applied__refuses() -> None:
    """Double-apply is a programmer error, not a silent no-op."""
    # given:
    applied = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=1,
        page=1,
        region="header block",
        reason="contains account number",
        author_id=ALICE,
        now=NOW,
    ).apply(now=NOW)

    # when/then:
    with pytest.raises(InvariantViolation, match="only proposed marks can be applied"):
        applied.apply(now=NOW)


def test_burn_as_rendition__applied_mark__new_redacted_rendition() -> None:
    """Burn produces a REDACTED rendition linked to the authorizing mark."""
    # given:
    applied = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=2,
        page=1,
        region="header block",
        reason="contains account number",
        author_id=ALICE,
        now=NOW,
    ).apply(now=NOW)

    # when:
    rendition = applied.burn_as_rendition(
        sha256="a" * 64,
        size=1024,
        storage_ref="renditions/redacted-1.bin",
        content_type="application/pdf",
        now=NOW,
    )

    # then:
    assert rendition.kind is RenditionKind.REDACTED
    assert rendition.source_version == 2
    assert rendition.redaction_id == applied.id
    # original mark is untouched — the original stays retrievable
    assert applied.status is RedactionStatus.APPLIED


def test_burn_as_rendition__proposed_mark__refuses() -> None:
    """Burning an unapproved proposal is refused (approval first)."""
    # given:
    proposed = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=1,
        page=1,
        region="header block",
        reason="contains account number",
        author_id=ALICE,
        now=NOW,
    )

    # when/then:
    with pytest.raises(InvariantViolation, match="only applied marks burn"):
        proposed.burn_as_rendition(
            sha256="a" * 64,
            size=10,
            storage_ref="r.bin",
            content_type="application/pdf",
            now=NOW,
        )


def test_supersede_redaction__older_mark__points_at_replacement() -> None:
    """Supersession preserves history via a replacement pointer."""
    # given:
    older = Redaction.propose(
        tenant_id=TENANT,
        document_id=DOC,
        base_version=1,
        page=1,
        region="header",
        reason="v1 region",
        author_id=ALICE,
        now=NOW,
    )
    newer_id = uuid4()

    # when:
    superseded = older.supersede(superseding_id=newer_id)

    # then:
    assert superseded.status is RedactionStatus.SUPERSEDED
    assert superseded.superseded_by == newer_id


# ---------------------------------------------------------------------------
# Rendition resolver
# ---------------------------------------------------------------------------


def test_resolve_viewer_rendition__redacted_available__prefers_redacted() -> None:
    """Least privilege: the viewer serves redacted bytes when allowed."""
    # given:
    mode = ViewerMode.PAGED

    # when:
    kind = resolve_viewer_rendition(
        mode=mode,
        has_redacted_rendition=True,
        may_view_original=True,
        may_view_redacted=True,
    )

    # then:
    assert kind is RenditionKind.REDACTED


def test_resolve_viewer_rendition__original_only__serves_original() -> None:
    """Without a burned redaction, the stricter right serves the original."""
    # given:
    mode = "paged"

    # when:
    kind = resolve_viewer_rendition(
        mode=mode,
        has_redacted_rendition=False,
        may_view_original=True,
        may_view_redacted=False,
    )

    # then:
    assert kind is RenditionKind.ORIGINAL


def test_resolve_viewer_rendition__no_rights__refuses_without_oracle() -> None:
    """A caller with neither right is refused (no existence leak)."""
    # given:
    mode = ViewerMode.DETAIL

    # when/then:
    with pytest.raises(PermissionDenied):
        resolve_viewer_rendition(
            mode=mode,
            has_redacted_rendition=True,
            may_view_original=False,
            may_view_redacted=False,
        )


def test_resolve_viewer_rendition__thumbnail_without_bytes__pending_burn() -> None:
    """A derived mode with no authorized bytes falls back, never leaks."""
    # given: redacted-view right but no burned rendition and no original right
    # when:
    kind = resolve_viewer_rendition(
        mode=ViewerMode.THUMBNAIL,
        has_redacted_rendition=False,
        may_view_original=False,
        may_view_redacted=True,
    )
    # then: the adapter renders "pending burn", not an unauthorized thumbnail
    assert kind is RenditionKind.REDACTED


def test_resolve_viewer_rendition__unknown_mode__rejects() -> None:
    """Unknown viewer modes are refused at the boundary."""
    # given:
    mode = "carousel"

    # when/then:
    with pytest.raises(ValidationFailed, match="unknown viewer mode"):
        resolve_viewer_rendition(
            mode=mode,
            has_redacted_rendition=False,
            may_view_original=True,
            may_view_redacted=True,
        )


# ---------------------------------------------------------------------------
# CheckoutLock — holder + expiry conflict contract
# ---------------------------------------------------------------------------


def test_acquire_checkout__fresh__live_lock() -> None:
    """A fresh acquire is live until its lease expires."""
    # given:
    now = NOW

    # when:
    lock = _lock(holder=ALICE, now=now)

    # then:
    assert lock.holder_user_id == ALICE
    assert lock.is_live(now=now) is True
    assert lock.is_live(now=now + timedelta(seconds=3600)) is False


def test_check_acquire__foreign_live_lock__conflict_carries_holder_and_expiry() -> None:
    """The T5 acceptance: conflicts return holder plus expiry."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when:
    with pytest.raises(CheckoutConflict) as excinfo:
        lock.check_acquire(requester_id=BOB, now=NOW + timedelta(minutes=1))

    # then:
    assert excinfo.value.holder == ALICE
    assert excinfo.value.expires_at == lock.expires_at
    details = excinfo.value.to_details()
    assert details["holder"] == str(ALICE)
    assert details["expires_at"] == lock.expires_at.isoformat()
    assert details["reason"] == "checkout_conflict"


def test_check_acquire__same_holder__allowed() -> None:
    """Re-acquire by the holder is idempotent (renew path, not conflict)."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when:
    lock.check_acquire(requester_id=ALICE, now=NOW + timedelta(minutes=1))

    # then: no raise
    assert True


def test_check_acquire__expired_lock__free_for_takeover() -> None:
    """An expired lock no longer blocks; takeover mints a fresh row."""
    # given:
    lock = _lock(holder=ALICE, now=NOW, lease=60)

    # when:
    lock.check_acquire(requester_id=BOB, now=NOW + timedelta(hours=1))

    # then: no raise
    assert True


def test_renew_checkout__holder__extends_expiry() -> None:
    """Only the holder may renew; renewal pushes the expiry forward."""
    # given:
    lock = _lock(holder=ALICE, now=NOW, lease=60)

    # when:
    renewed = lock.renew(requester_id=ALICE, now=NOW + timedelta(seconds=30), lease_seconds=3600)

    # then:
    assert renewed.expires_at == NOW + timedelta(seconds=30) + timedelta(seconds=3600)


def test_renew_checkout__foreign_holder__conflict_with_holder() -> None:
    """A foreign renew learns the true holder + expiry, not just denied."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when/then:
    with pytest.raises(CheckoutConflict) as excinfo:
        lock.renew(requester_id=BOB, now=NOW + timedelta(minutes=1), lease_seconds=60)
    assert excinfo.value.holder == ALICE


def test_renew_checkout__expired__refuses() -> None:
    """Renewing an expired lock is refused; acquire fresh for history."""
    # given:
    lock = _lock(holder=ALICE, now=NOW, lease=60)

    # when/then:
    with pytest.raises(InvariantViolation, match="expired.*acquire fresh"):
        lock.renew(requester_id=ALICE, now=NOW + timedelta(hours=1), lease_seconds=60)


def test_release_checkout__holder__released() -> None:
    """Holder release ends the lock; the row stays for checkout-history."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when:
    released = lock.release(requester_id=ALICE, now=NOW + timedelta(minutes=5))

    # then:
    assert released.released_at == NOW + timedelta(minutes=5)
    assert released.is_live(now=NOW + timedelta(minutes=6)) is False


def test_release_checkout__foreign_holder__conflict() -> None:
    """Only the holder may release; others get holder + expiry."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when/then:
    with pytest.raises(CheckoutConflict) as excinfo:
        lock.release(requester_id=BOB, now=NOW + timedelta(minutes=1))
    assert excinfo.value.holder == ALICE
    assert excinfo.value.expires_at == lock.expires_at


def test_acquire_checkout__oversize_lease__rejects() -> None:
    """Runaway leases are bounded at construction."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="lease_seconds must be <="):
        CheckoutLock.acquire(
            tenant_id=TENANT,
            document_id=DOC,
            holder_user_id=ALICE,
            now=now,
            lease_seconds=8 * 24 * 60 * 60,
        )


# ---------------------------------------------------------------------------
# Concurrent-edit rule (pure)
# ---------------------------------------------------------------------------


def test_evaluate_edit_lock__no_lock__allowed() -> None:
    """No checkout row means the document is free to edit."""
    # given:
    active: CheckoutLock | None = None

    # when:
    decision: EditLockDecision = evaluate_edit_lock(active_lock=active, requester_id=BOB, now=NOW)

    # then:
    assert decision.allowed is True
    assert decision.reason == "no_live_checkout"


def test_evaluate_edit_lock__own_live_checkout__allowed() -> None:
    """The holder may edit under their own checkout."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when:
    decision = evaluate_edit_lock(active_lock=lock, requester_id=ALICE, now=NOW)

    # then:
    assert decision.allowed is True
    assert decision.reason == "own_checkout"


def test_evaluate_edit_lock__foreign_live_checkout__denied_with_holder() -> None:
    """A foreign live checkout denies edits and names holder + expiry."""
    # given:
    lock = _lock(holder=ALICE, now=NOW)

    # when:
    decision = evaluate_edit_lock(active_lock=lock, requester_id=BOB, now=NOW)

    # then:
    assert decision.allowed is False
    assert decision.holder == ALICE
    assert decision.expires_at == lock.expires_at


def test_evaluate_edit_lock__expired_checkout__allowed() -> None:
    """An expired checkout no longer gates edits."""
    # given:
    lock = _lock(holder=ALICE, now=NOW, lease=60)

    # when:
    decision = evaluate_edit_lock(active_lock=lock, requester_id=BOB, now=NOW + timedelta(hours=1))

    # then:
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# ReindexRequest
# ---------------------------------------------------------------------------


def test_request_reindex__valid__constructs() -> None:
    """A reasoned re-index request validates cleanly."""
    # given:
    now = NOW

    # when:
    req = ReindexRequest.request(
        tenant_id=TENANT,
        document_id=DOC,
        requested_by=ALICE,
        reason="post-edit catalog refresh",
        now=now,
    )

    # then:
    assert req.document_id == DOC
    assert req.reason == "post-edit catalog refresh"


def test_request_reindex__blank_reason__rejects() -> None:
    """The rebuild audit needs a rationale, so blank reasons are refused."""
    # given:
    now = NOW

    # when/then:
    with pytest.raises(ValidationFailed, match="reason is required"):
        ReindexRequest.request(
            tenant_id=TENANT,
            document_id=DOC,
            requested_by=ALICE,
            reason="   ",
            now=now,
        )
