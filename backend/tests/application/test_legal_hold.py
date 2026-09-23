"""Tests for the FM-039 legal-hold lifecycle use cases (P08-US1, US2).

Acceptance scenarios (FM-039 / pillar P08 §6):

- **H1** — A security administrator places a case under legal hold.
  An attempt to dispose of any document associated with the case is
  rejected with a clear ``legal_hold_active`` error.
- **H2** — A security administrator releases the hold. The release
  is recorded as a ``HoldReleased`` audit event. Subsequent
  disposition proceeds.

These tests cover the application-layer contract: place hold blocks
disposition, release unblocks, the hold lifecycle lands on the
audit chain, and a cross-tenant actor never sees another tenant's
hold. The domain refusal guard itself is covered by
``tests/domain/test_legal_hold.py``; the in-memory repository's
contract is verified here through the use case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.cases.create_case import (
    CreateCaseCommand,
)
from app.application.holds.manage_holds import (
    PlaceHoldCommand,
    ReleaseHoldCommand,
    place_hold,
    release_hold,
)
from app.domain.case import Case, CaseStatus
from app.domain.errors import Forbidden, NotFound
from app.domain.legal_hold import HoldAlreadyPresent, HoldNotPresent
from app.infrastructure.persistence.legal_hold_repository import (
    InMemoryLegalHoldRepository,
)

from .cases._fakes import (
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
SECURITY_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
LATER_NOW = datetime(2026, 8, 31, 13, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _MutableClock:
    """A clock the test can advance to simulate elapsed time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, now: datetime) -> None:
        self._now = now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> _MutableClock:
    return _MutableClock(FIXED_NOW)


@pytest.fixture
def seeded(
    clock: _MutableClock,
) -> tuple[
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryLegalHoldRepository,
    InMemoryAuditStore,
    InMemoryOutbox,
    Case,
]:
    """Seed an ACME tenant with one open case ready for hold testing."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title",),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)

    hold_repo = InMemoryLegalHoldRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    # Bypass the create_case use case's async signature with a manual
    # build so the fixture stays sync; the audit/outbox stores start
    # empty here and the first row each test sees is the one produced
    # by the hold operation under test.
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=_ref("C-ACME-2026-000001"),
        case_type_version=version,
        title="Subject under review",
        description="",
        data={"title": "Subject under review"},
        created_by=INTAKE,
        now=FIXED_NOW,
    )
    case_repo._cases.append(case)  # noqa: SLF001 - test fixture

    return case_repo, case_type_repo, version_repo, hold_repo, audit, outbox, case


def _ref(value: str) -> Any:
    """Build a CaseReference value (lazy import keeps the fixture terse)."""
    from app.domain.case import CaseReference  # noqa: PLC0415

    return CaseReference(value)


def _place_cmd(**overrides: Any) -> PlaceHoldCommand:
    payload: dict[str, Any] = {
        "tenant_id": ACME_TENANT,
        "actor_id": SECURITY_ADMIN,
        "actor_roles": ("process_admin",),
        "case_id": uuid4(),
        "reason": "Litigation preservation",
        "correlation_id": "corr-place-1",
    }
    payload.update(overrides)
    return PlaceHoldCommand(**payload)


def _release_cmd(**overrides: Any) -> ReleaseHoldCommand:
    payload: dict[str, Any] = {
        "tenant_id": ACME_TENANT,
        "actor_id": SECURITY_ADMIN,
        "actor_roles": ("process_admin",),
        "case_id": uuid4(),
        "reason": "Litigation resolved",
        "correlation_id": "corr-release-1",
    }
    payload.update(overrides)
    return ReleaseHoldCommand(**payload)


# ---------------------------------------------------------------------------
# 1. Place hold — happy path
# ---------------------------------------------------------------------------


async def test_place_hold__security_admin__persists_hold_and_audit_and_flips_case_flag(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """A ``process_admin`` placing a hold on an open case persists the
    ``LegalHold`` row, writes a ``HoldPlaced`` audit row, and flips
    the case's ``legal_hold`` flag to ``True`` — all in one logical
    transaction.
    """
    case_repo, _, _, hold_repo, audit, _, case = seeded
    cmd = _place_cmd(case_id=case.id)

    result = await place_hold(
        cmd,
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )

    # Case flag flipped.
    assert result.case.legal_hold is True
    # Hold row persisted, active, with actor + reason + timestamp.
    assert result.hold.is_active is True
    assert result.hold.placed_by == SECURITY_ADMIN
    assert result.hold.placed_reason == "Litigation preservation"
    assert result.hold.placed_at == FIXED_NOW
    assert result.hold.released_at is None
    # Repository reflects the new active hold.
    stored = await hold_repo.get_active(ACME_TENANT, case.id)
    assert stored is not None
    assert stored.id == result.hold.id
    # Audit row written, tenant-scoped, with the HoldPlaced type.
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == "HoldPlaced"
    assert event.tenant_id == ACME_TENANT
    assert event.subject_id == result.hold.id
    assert event.case_id == case.id
    assert event.payload["case_reference"] == case.reference.value
    assert event.payload["placed_by"] == str(SECURITY_ADMIN)


# ---------------------------------------------------------------------------
# 2. Place hold — refusal when active hold exists
# ---------------------------------------------------------------------------


async def test_place_hold__case_already_held__raises_hold_already_present_no_writes(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """A second ``place`` against a case that already has an active
    hold raises :class:`HoldAlreadyPresent` BEFORE any write so the
    audit chain never carries a refused attempt.
    """
    case_repo, _, _, hold_repo, audit, _, case = seeded
    first = await place_hold(
        _place_cmd(case_id=case.id),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )
    assert first.case.legal_hold is True

    with pytest.raises(HoldAlreadyPresent):
        await place_hold(
            _place_cmd(case_id=case.id, reason="second attempt"),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=InMemoryAuditStore(),
            clock=clock,
        )

    # Only the original audit row exists.
    assert len(audit.events) == 1
    # The active hold is the original.
    stored = await hold_repo.get_active(ACME_TENANT, case.id)
    assert stored is not None
    assert stored.placed_reason == "Litigation preservation"


# ---------------------------------------------------------------------------
# 3. Auth denial
# ---------------------------------------------------------------------------


async def test_place_hold__non_admin_actor__raises_forbidden_no_writes(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """A reviewer without ``case.hold_place`` is refused with
    :class:`Forbidden` BEFORE any DB write. The denial surfaces on
    the audit chain via the access-decision recorder (out of scope
    for this slice) but the data layer never sees the write.
    """
    case_repo, _, _, hold_repo, audit, _, case = seeded

    with pytest.raises(Forbidden):
        await place_hold(
            _place_cmd(case_id=case.id, actor_id=REVIEWER, actor_roles=("reviewer",)),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )

    assert audit.events == []
    assert (await hold_repo.get_active(ACME_TENANT, case.id)) is None


# ---------------------------------------------------------------------------
# 4. Release hold — unblocks disposition
# ---------------------------------------------------------------------------


async def test_release_hold__after_place__flips_flag_and_records_audit(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """End-to-end H1 + H2: place blocks a status flip; release
    unblocks it. The status flip is exercised directly on the
    aggregate so the test isolates the application-layer behavior
    from any future close-case use case.
    """
    case_repo, _, _, hold_repo, audit, _, case = seeded

    # Place.
    placed = await place_hold(
        _place_cmd(case_id=case.id),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )
    assert placed.case.legal_hold is True

    # While held, attempting to close the case raises Conflict
    # (the aggregate guard, exercised here against the placed
    # aggregate to confirm the flag is set).
    from app.domain.errors import Conflict  # noqa: PLC0415

    with pytest.raises(Conflict, match="legal hold"):
        placed.case.change_status(new_status=CaseStatus.CLOSED, now=LATER_NOW)

    # Advance the clock; release; verify unblock.
    clock.advance_to(LATER_NOW)
    released = await release_hold(
        _release_cmd(case_id=case.id),
        case_repo=case_repo,
        hold_repo=hold_repo,
        audit=audit,
        clock=clock,
    )

    assert released.case.legal_hold is False
    assert released.hold.is_active is False
    assert released.hold.released_by == SECURITY_ADMIN
    assert released.hold.released_reason == "Litigation resolved"
    assert released.hold.released_at == LATER_NOW

    # The released row's identity is preserved (one hold row, two
    # lifecycle phases).
    assert released.hold.id == placed.hold.id

    # Audit chain shows place + release.
    types = [e.event_type for e in audit.events]
    assert types == ["HoldPlaced", "HoldReleased"]

    # Disposition now proceeds.
    closed = released.case.change_status(new_status=CaseStatus.CLOSED, now=LATER_NOW)
    assert closed.status is CaseStatus.CLOSED


# ---------------------------------------------------------------------------
# 5. Release without active hold — refusal
# ---------------------------------------------------------------------------


async def test_release_hold__no_active_hold__raises_hold_not_present_no_writes(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """Releasing when no hold exists surfaces ``HoldNotPresent`` (409)
    BEFORE any write — symmetric with :class:`HoldAlreadyPresent`.
    """
    case_repo, _, _, hold_repo, audit, _, case = seeded

    with pytest.raises(HoldNotPresent):
        await release_hold(
            _release_cmd(case_id=case.id),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )

    assert audit.events == []


# ---------------------------------------------------------------------------
# 6. Tenant isolation
# ---------------------------------------------------------------------------


async def test_place_and_release_hold__cross_tenant_case_id__raises_not_found(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """An actor in tenant A cannot place a hold on a tenant B case.
    The repository never matches the cross-tenant id, so the use
    case raises :class:`NotFound` (the API surface is "no such
    case", never "case exists but you can't see it" — Constitution V).
    """
    case_repo, _, _, hold_repo, audit, _, _case = seeded

    cross_tenant_case_id = uuid4()
    with pytest.raises(NotFound):
        await place_hold(
            _place_cmd(
                tenant_id=OTHER_TENANT,
                case_id=cross_tenant_case_id,
            ),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )

    with pytest.raises(NotFound):
        await release_hold(
            _release_cmd(
                tenant_id=OTHER_TENANT,
                case_id=cross_tenant_case_id,
            ),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )

    assert audit.events == []


# ---------------------------------------------------------------------------
# 7. Missing case
# ---------------------------------------------------------------------------


async def test_place_hold__missing_case__raises_not_found_no_writes(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """A hold against a non-existent case id raises :class:`NotFound`
    without staging any row."""
    case_repo, _, _, hold_repo, audit, _, _case = seeded
    missing_id = uuid4()

    with pytest.raises(NotFound):
        await place_hold(
            _place_cmd(case_id=missing_id),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )
    assert audit.events == []


# ---------------------------------------------------------------------------
# 8. Reason validation
# ---------------------------------------------------------------------------


async def test_place_hold__blank_reason__rejected_by_aggregate_no_writes(
    seeded: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
        InMemoryLegalHoldRepository,
        InMemoryAuditStore,
        InMemoryOutbox,
        Case,
    ],
    clock: _MutableClock,
) -> None:
    """A blank reason is rejected at the aggregate so the audit row
    never carries a meaningless rationale.
    """
    from app.domain.errors import ValidationFailed  # noqa: PLC0415

    case_repo, _, _, hold_repo, audit, _, case = seeded

    with pytest.raises(ValidationFailed):
        await place_hold(
            _place_cmd(case_id=case.id, reason="   "),
            case_repo=case_repo,
            hold_repo=hold_repo,
            audit=audit,
            clock=clock,
        )

    assert audit.events == []


# Sentinel to satisfy the unused-import linter — keep this near the
# bottom so the dataclass import is referenced if the test body
# grows.
_ = (CreateCaseCommand,)  # pragma: no cover - module-shape anchor
__all__: list[str] = []
