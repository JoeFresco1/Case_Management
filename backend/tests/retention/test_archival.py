"""Tests for the FM-041 archival + freeze + orphan use cases (P08-US5, US7, US8).

Acceptance scenarios (FM-041 / pillar P08 §6):

- An admin archives a case in a cooler tier; the case is frozen —
  any disposition approval against the case is refused while the
  archival row is in ``archived`` status.
- An admin declares a tenant-wide disposal freeze window; an
  attempt to approve a disposition inside the window is refused
  with a structured ``disposal_frozen`` error.
- The orphan-detection scan surfaces reviewable proposals for
  cases past their retention horizon AND with no recent
  activity; cases under active legal hold are skipped (the
  pillar's "hold overrides retention" invariant is preserved).
- The scan never auto-deletes a case; a reviewer either surfaces
  the proposal (the case moves into the normal disposition
  pipeline) or dismisses it with a required rationale.

These tests cover the application-layer contract end-to-end
through the use cases (archive, unfreeze, declare freeze,
cancel freeze, propose orphans, review proposal, freeze
blocking) plus the pure-function helper from
:func:`app.domain.retention.is_freeze_blocking`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    place_hold,
)
from app.application.retention.archival import (
    ArchiveCaseCommand,
    CancelDisposalFreezeCommand,
    DeclareDisposalFreezeCommand,
    ReviewOrphanProposalCommand,
    UnfreezeCaseCommand,
    archive_case,
    cancel_disposal_freeze,
    declare_disposal_freeze,
    is_disposition_blocked,
    list_active_freezes,
    propose_orphan_cases,
    review_orphan_proposal,
    unfreeze_case,
)
from app.domain.case import Case
from app.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.retention import (
    ORPHAN_INACTIVITY_DAYS,
    ArchivalTier,
    DisposalFreeze,
    is_freeze_blocking,
)
from app.infrastructure.persistence.legal_hold_repository import (
    InMemoryLegalHoldRepository,
)
from app.infrastructure.persistence.retention_repository import (
    InMemoryCaseArchivalRepository,
    InMemoryDisposalFreezeRepository,
    InMemoryOrphanCaseProposalRepository,
)
from tests.application.cases._fakes import (
    InMemoryAuditStore,
    InMemoryCaseRepository,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
SECURITY_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
STALE_ACTIVITY = NOW - timedelta(days=ORPHAN_INACTIVITY_DAYS + 30)
RECENT_ACTIVITY = NOW - timedelta(days=1)


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _MutableClock:
    """A clock the test can advance to simulate elapsed time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, now: datetime) -> None:
        self._now = now


@pytest.fixture
def clock() -> _MutableClock:
    return _MutableClock(NOW)


async def _build_case(
    case_repo: InMemoryCaseRepository,
    *,
    tenant_id: UUID,
    created_at: datetime,
    updated_at: datetime,
) -> Case:
    """Build a backdated case ready for archival / orphan tests."""
    case_repo.set_tenant_short(tenant_id, "ACME" if tenant_id == ACME_TENANT else "OTHER")
    _, version = make_case_type(tenant_id=tenant_id, required_keys=("title", "priority"))
    reference = await case_repo.next_reference(tenant_id, now=created_at)
    return Case.create(
        tenant_id=tenant_id,
        reference=reference,
        case_type_version=version,
        title="Archive candidate",
        description="",
        data={"title": "Archive candidate", "priority": "normal"},
        priority="normal",
        created_by=INTAKE,
        now=created_at,
    )


@pytest.fixture
def case_repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def archival_repo() -> InMemoryCaseArchivalRepository:
    return InMemoryCaseArchivalRepository()


@pytest.fixture
def freeze_repo() -> InMemoryDisposalFreezeRepository:
    return InMemoryDisposalFreezeRepository()


@pytest.fixture
def orphan_repo() -> InMemoryOrphanCaseProposalRepository:
    return InMemoryOrphanCaseProposalRepository()


@pytest.fixture
def hold_repo() -> InMemoryLegalHoldRepository:
    return InMemoryLegalHoldRepository()


# ---------------------------------------------------------------------------
# archive_case — P08-US5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_case__moves_to_warm_tier__audits(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """An admin archives a case; the row is created in ``archived`` status."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
    )
    await case_repo.add(case)

    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="case is past its active lifecycle",
    )
    result = await archive_case(
        cmd,
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit_store,
        clock=clock,
    )

    assert result.archival.is_frozen is True
    assert result.archival.tier is ArchivalTier.WARM
    assert result.archival.reason == "case is past its active lifecycle"
    assert audit_store.events[-1].event_type == "CaseArchived"
    # The case itself is not mutated by the use case; the freeze
    # is encoded on the archival row.
    fetched = await case_repo.get(ACME_TENANT, case.id)
    assert fetched is not None


@pytest.mark.asyncio
async def test_archive_case__rejects_hot_tier(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """Archiving to the HOT tier is refused (HOT is the default mutable tier)."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)

    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.HOT,
        reason="misconfigured",
    )
    with pytest.raises(ValidationFailed):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_archive_case__rejects_blank_reason(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A blank reason is refused (Constitution IV — audit must carry the *why*)."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)

    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="   ",
    )
    with pytest.raises(ValidationFailed):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_archive_case__rejects_already_archived(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A second archive on the same case is refused with ``Conflict``."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)

    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="first move",
    )
    await archive_case(
        cmd,
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit_store,
        clock=clock,
    )

    with pytest.raises(Conflict):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_archive_case__rejects_missing_case(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A missing case surfaces as ``NotFound`` (Constitution V)."""
    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=uuid4(),
        tier=ArchivalTier.WARM,
        reason="missing",
    )
    with pytest.raises(NotFound):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# unfreeze_case — P08-US5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unfreeze_case__lifts_freeze__audits(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """An admin unfreezes a previously archived case."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
    )
    await case_repo.add(case)

    archive_cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="first move",
    )
    await archive_case(
        archive_cmd,
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit_store,
        clock=clock,
    )

    unfreeze_cmd = UnfreezeCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        reason="case needs to be re-opened",
    )
    result = await unfreeze_case(
        unfreeze_cmd,
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit_store,
        clock=clock,
    )

    assert result.archival.is_frozen is False
    assert result.archival.unfreeze_reason == "case needs to be re-opened"
    assert audit_store.events[-1].event_type == "CaseUnfrozen"
    # The active slot is vacated so a future archive rebinds it.
    active = await archival_repo.get_active_for_case(ACME_TENANT, case.id)
    assert active is None


@pytest.mark.asyncio
async def test_unfreeze_case__rejects_non_archived(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """Unfreeze against a non-archived case is a process error (NotFound)."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)

    cmd = UnfreezeCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        reason="misclick",
    )
    with pytest.raises(NotFound):
        await unfreeze_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Disposal freeze — P08-US7
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freeze_blocking_helper__active_freeze_returns_true() -> None:
    """An active freeze blocks the snapshot moment."""
    freeze = DisposalFreeze.declare(
        tenant_id=ACME_TENANT,
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1),
        reason="litigation hold",
        created_by=SECURITY_ADMIN,
        created_at=NOW - timedelta(hours=1),
    )
    assert is_freeze_blocking(freezes=[freeze], at=NOW) is True


@pytest.mark.asyncio
async def test_freeze_blocking_helper__expired_freeze_returns_false() -> None:
    """A naturally expired freeze is inactive without a separate state flip."""
    freeze = DisposalFreeze.declare(
        tenant_id=ACME_TENANT,
        starts_at=NOW - timedelta(hours=2),
        ends_at=NOW - timedelta(hours=1),
        reason="expired",
        created_by=SECURITY_ADMIN,
        created_at=NOW - timedelta(hours=2),
    )
    assert is_freeze_blocking(freezes=[freeze], at=NOW) is False


@pytest.mark.asyncio
async def test_freeze_blocking_helper__cancelled_freeze_returns_false() -> None:
    """A cancelled freeze is inactive."""
    freeze = DisposalFreeze.declare(
        tenant_id=ACME_TENANT,
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1),
        reason="litigation hold",
        created_by=SECURITY_ADMIN,
        created_at=NOW - timedelta(hours=1),
    )
    cancelled = freeze.cancel(
        actor_id=SECURITY_ADMIN,
        at=NOW,
        reason="litigation resolved",
    )
    assert is_freeze_blocking(freezes=[cancelled], at=NOW) is False


@pytest.mark.asyncio
async def test_declare_disposal_freeze__creates_window(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A freeze declaration creates an active row + audit entry."""
    cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=24),
        reason="regulator inquiry",
    )
    result = await declare_disposal_freeze(
        cmd,
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )
    assert result.freeze.is_active_at(at=NOW) is True
    assert audit_store.events[-1].event_type == "DisposalFreezeDeclared"


@pytest.mark.asyncio
async def test_declare_disposal_freeze__rejects_zero_length_window(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A zero-length window is refused so a misconfigured admin cannot freeze forever."""
    cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW,
        ends_at=NOW,
        reason="oops",
    )
    with pytest.raises(ValidationFailed):
        await declare_disposal_freeze(
            cmd,
            freeze_repo=freeze_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_declare_disposal_freeze__rejects_blank_reason(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A blank reason is refused."""
    cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        reason="   ",
    )
    with pytest.raises(ValidationFailed):
        await declare_disposal_freeze(
            cmd,
            freeze_repo=freeze_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_cancel_disposal_freeze__lifts_freeze_early(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A cancel lifts the freeze; the audit row records who + why."""
    declare_cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=24),
        reason="regulator inquiry",
    )
    declared = await declare_disposal_freeze(
        declare_cmd,
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )

    cancel_cmd = CancelDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        freeze_id=declared.freeze.id,
        reason="inquiry resolved",
    )
    result = await cancel_disposal_freeze(
        cancel_cmd,
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )
    assert result.freeze.cancelled_at is not None
    assert result.freeze.is_active_at(at=NOW) is False
    assert audit_store.events[-1].event_type == "DisposalFreezeCancelled"


@pytest.mark.asyncio
async def test_cancel_disposal_freeze__rejects_double_cancel(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A second cancellation on the same freeze is refused with ``Conflict``."""
    declare_cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=24),
        reason="regulator inquiry",
    )
    declared = await declare_disposal_freeze(
        declare_cmd,
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )

    cancel_cmd = CancelDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        freeze_id=declared.freeze.id,
        reason="first cancel",
    )
    await cancel_disposal_freeze(
        cancel_cmd,
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )

    with pytest.raises(Conflict):
        await cancel_disposal_freeze(
            cancel_cmd,
            freeze_repo=freeze_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_is_disposition_blocked__active_freeze_returns_true(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    freeze_repo: InMemoryDisposalFreezeRepository,
    clock: _MutableClock,
) -> None:
    """An active freeze blocks disposition approval."""
    declare_cmd = DeclareDisposalFreezeCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1),
        reason="litigation",
    )
    await declare_disposal_freeze(
        declare_cmd,
        freeze_repo=freeze_repo,
        audit=InMemoryAuditStore(),
        clock=clock,
    )

    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    blocked = await is_disposition_blocked(
        tenant_id=ACME_TENANT,
        freeze_repo=freeze_repo,
        case_archival_repo=archival_repo,
        case_id=case.id,
        clock=clock,
    )
    assert blocked is True


@pytest.mark.asyncio
async def test_is_disposition_blocked__archived_case_returns_true(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """An archived case is frozen; disposition is blocked even without a freeze."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
    )
    await case_repo.add(case)
    archive_cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="archive",
    )
    await archive_case(
        archive_cmd,
        case_repo=case_repo,
        archival_repo=archival_repo,
        audit=audit_store,
        clock=clock,
    )

    blocked = await is_disposition_blocked(
        tenant_id=ACME_TENANT,
        freeze_repo=freeze_repo,
        case_archival_repo=archival_repo,
        case_id=case.id,
        clock=clock,
    )
    assert blocked is True


@pytest.mark.asyncio
async def test_list_active_freezes__returns_only_active(
    freeze_repo: InMemoryDisposalFreezeRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """The active-snapshot helper returns only the in-window rows."""
    active = await declare_disposal_freeze(
        DeclareDisposalFreezeCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1),
            reason="active",
        ),
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )
    # An expired freeze that was never cancelled.
    await declare_disposal_freeze(
        DeclareDisposalFreezeCommand(
            tenant_id=ACME_TENANT,
            actor_id=SECURITY_ADMIN,
            actor_roles=("process_admin",),
            starts_at=NOW - timedelta(hours=3),
            ends_at=NOW - timedelta(hours=2),
            reason="expired",
        ),
        freeze_repo=freeze_repo,
        audit=audit_store,
        clock=clock,
    )

    snapshot = await list_active_freezes(
        tenant_id=ACME_TENANT,
        freeze_repo=freeze_repo,
        clock=clock,
    )
    assert len(snapshot.freezes) == 1
    assert snapshot.freezes[0].id == active.freeze.id


# ---------------------------------------------------------------------------
# Orphan proposals — P08-US8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_orphan_cases__stages_reviewable_proposal(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    clock: _MutableClock,
) -> None:
    """A stale case is staged as a reviewable proposal — NOT auto-deleted."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)

    result = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )

    assert len(result.proposals) == 1
    entry = result.proposals[0]
    assert entry.case_id == stale.id
    assert entry.proposal.is_open is True
    # The case itself is not mutated.
    fetched = await case_repo.get(ACME_TENANT, stale.id)
    assert fetched is not None


@pytest.mark.asyncio
async def test_propose_orphan_cases__skips_recently_active(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    clock: _MutableClock,
) -> None:
    """A case with recent activity is not surfaced (not stale enough)."""
    recent = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW - timedelta(days=2),
        updated_at=RECENT_ACTIVITY,
    )
    await case_repo.add(recent)

    result = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    assert result.proposals == []


@pytest.mark.asyncio
async def test_propose_orphan_cases__skips_cases_under_legal_hold(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A held case is never an orphan candidate (hold overrides retention)."""
    held = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(held)

    place_cmd = PlaceHoldCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=held.id,
        reason="litigation",
    )
    await place_hold(
        place_cmd,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit_store,
        clock=clock,
    )

    result = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    assert result.proposals == []
    assert held.id in result.skipped_holds


@pytest.mark.asyncio
async def test_propose_orphan_cases__idempotent_against_rerun(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    clock: _MutableClock,
) -> None:
    """Re-running the scan does not produce duplicate open proposals."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)

    first = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    second = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    assert len(first.proposals) == 1
    assert second.proposals == []


@pytest.mark.asyncio
async def test_review_orphan_proposal__surface(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A surface decision moves the proposal to ``surfaced`` status."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)
    scan = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    proposal_id = scan.proposals[0].proposal.id

    cmd = ReviewOrphanProposalCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        proposal_id=proposal_id,
        decision="surface",
        reason="",
    )
    result = await review_orphan_proposal(
        cmd,
        proposal_repo=orphan_repo,
        audit=audit_store,
        clock=clock,
    )
    assert result.proposal.status.value == "surfaced"
    assert audit_store.events[-1].event_type == "OrphanProposalSurfaced"


@pytest.mark.asyncio
async def test_review_orphan_proposal__dismiss_requires_reason(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A dismiss without a rationale is refused (Constitution IV)."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)
    scan = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    proposal_id = scan.proposals[0].proposal.id

    cmd = ReviewOrphanProposalCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        proposal_id=proposal_id,
        decision="dismiss",
        reason="   ",
    )
    with pytest.raises(ValidationFailed):
        await review_orphan_proposal(
            cmd,
            proposal_repo=orphan_repo,
            audit=audit_store,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_review_orphan_proposal__dismiss_with_reason(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A dismiss with a rationale records the dismissal in the audit chain."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)
    scan = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    proposal_id = scan.proposals[0].proposal.id

    cmd = ReviewOrphanProposalCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        proposal_id=proposal_id,
        decision="dismiss",
        reason="case is still in active litigation under a different hold",
    )
    result = await review_orphan_proposal(
        cmd,
        proposal_repo=orphan_repo,
        audit=audit_store,
        clock=clock,
    )
    assert result.proposal.status.value == "dismissed"
    assert (
        result.proposal.review_reason == "case is still in active litigation under a different hold"
    )
    assert audit_store.events[-1].event_type == "OrphanProposalDismissed"


@pytest.mark.asyncio
async def test_review_orphan_proposal__rejects_already_decided(
    case_repo: InMemoryCaseRepository,
    hold_repo: InMemoryLegalHoldRepository,
    orphan_repo: InMemoryOrphanCaseProposalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A second decision on a decided proposal is refused with ``Conflict``."""
    stale = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=STALE_ACTIVITY - timedelta(days=10),
        updated_at=STALE_ACTIVITY,
    )
    await case_repo.add(stale)
    scan = await propose_orphan_cases(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_repo=case_repo,
        hold_repo=hold_repo,
        proposal_repo=orphan_repo,
        clock=clock,
    )
    proposal_id = scan.proposals[0].proposal.id

    surface_cmd = ReviewOrphanProposalCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        proposal_id=proposal_id,
        decision="surface",
        reason="",
    )
    await review_orphan_proposal(
        surface_cmd,
        proposal_repo=orphan_repo,
        audit=audit_store,
        clock=clock,
    )

    with pytest.raises(Conflict):
        await review_orphan_proposal(
            ReviewOrphanProposalCommand(
                tenant_id=ACME_TENANT,
                actor_id=SECURITY_ADMIN,
                actor_roles=("process_admin",),
                proposal_id=proposal_id,
                decision="dismiss",
                reason="late reversal",
            ),
            proposal_repo=orphan_repo,
            audit=audit_store,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Authorization — case workers cannot archive / declare freeze / etc.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_case__rejects_worker_role(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A case worker is denied (the records-admin action is process_admin only)."""
    case = await _build_case(
        case_repo,
        tenant_id=ACME_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)
    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=INTAKE,
        actor_roles=("intake",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="I am a worker",
    )
    with pytest.raises(Forbidden):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_case__rejects_cross_tenant_case(
    case_repo: InMemoryCaseRepository,
    archival_repo: InMemoryCaseArchivalRepository,
    audit_store: InMemoryAuditStore,
    clock: _MutableClock,
) -> None:
    """A cross-tenant case id surfaces as ``NotFound`` (Constitution V)."""
    case = await _build_case(
        case_repo,
        tenant_id=OTHER_TENANT,
        created_at=NOW,
        updated_at=NOW,
    )
    await case_repo.add(case)
    cmd = ArchiveCaseCommand(
        tenant_id=ACME_TENANT,
        actor_id=SECURITY_ADMIN,
        actor_roles=("process_admin",),
        case_id=case.id,
        tier=ArchivalTier.WARM,
        reason="cross-tenant",
    )
    with pytest.raises(NotFound):
        await archive_case(
            cmd,
            case_repo=case_repo,
            archival_repo=archival_repo,
            audit=audit_store,
            clock=clock,
        )
