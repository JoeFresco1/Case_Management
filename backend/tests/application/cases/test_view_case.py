"""Tests for `app.application.cases.view_case.handle` (P01-US2, FM-006, FR-002).

Acceptance scenarios from `specs/001-case-management-platform/first-slice/spec.md`
US2, FR-002:

1. **Authorized read**: an actor with `case.read` permission sees the
   full case summary.
2. **Unauthorized read**: an actor without any role in the tenant is
   denied (deny-by-default, AGENTS.md §4.R12).
3. **Cross-tenant id**: a case id that lives in another tenant surfaces
   as `NotFound` (the repository filters by tenant_id; the use case
   never reveals cross-tenant existence).
4. **Missing case**: a non-existent case id raises `NotFound`.
5. **Sensitive redaction**: when a case is marked sensitive, structured
   `data` is stripped so the actor cannot learn the content. The
   reference / status / type remain visible so the operator still
   knows the case exists.

Naming follows `test_<unit>_<scenario>__<expectation>` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.cases.summary import (
    build_summary,
    summary_to_dict,
    summary_to_list_dict,
)
from app.application.cases.view_case import (
    MAX_PAGE_LIMIT,
    Redacted,
    ViewCaseQuery,
    _redacted,
)
from app.application.cases.view_case import (
    handle as view_case,
)
from app.domain.case import Case, CaseReference
from app.domain.errors import Forbidden, NotFound
from app.domain.ports import CaseRepository

from ._fakes import InMemoryCaseRepository, make_case_type

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
GHOST_USER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_case() -> tuple[InMemoryCaseRepository, Case]:
    """A persisted case under ACME_TENANT for the view_case tests."""
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type, version = make_case_type(tenant_id=ACME_TENANT)
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Help with onboarding",
        description="First-time customer request.",
        data={"title": "Help with onboarding", "priority": "normal"},
        priority="normal",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )
    repo._cases.append(case)  # bypass add() so we can keep the exact id
    return repo, case


# ---------------------------------------------------------------------------
# 1. Authorized read
# ---------------------------------------------------------------------------


async def test_view_case__authorized_actor__returns_domain_case(
    seeded_case: tuple[InMemoryCaseRepository, Case],
) -> None:
    """An intake user with ``case.read`` permission receives the case."""
    repo, case = seeded_case
    returned = await view_case(
        ViewCaseQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            case_id=case.id,
        ),
        case_repo=repo,
    )
    assert returned.id == case.id
    assert returned.reference.value == "C-ACME-2026-000001"
    assert returned.status.value == "open"


# ---------------------------------------------------------------------------
# 2. Unauthorized read
# ---------------------------------------------------------------------------


async def test_view_case__actor_without_any_role__raises_forbidden(
    seeded_case: tuple[InMemoryCaseRepository, Case],
) -> None:
    """An actor with empty ``roles`` is denied by default (P11-US3 / R12)."""
    repo, case = seeded_case
    with pytest.raises(Forbidden):
        await view_case(
            ViewCaseQuery(
                tenant_id=ACME_TENANT,
                actor_id=GHOST_USER,
                actor_roles=(),
                case_id=case.id,
            ),
            case_repo=repo,
        )


# ---------------------------------------------------------------------------
# 3. Cross-tenant id
# ---------------------------------------------------------------------------


async def test_view_case__cross_tenant_id__raises_notfound(
    seeded_case: tuple[InMemoryCaseRepository, Case],
) -> None:
    """A case id from another tenant surfaces as NotFound (no leak)."""
    repo, case = seeded_case
    with pytest.raises(NotFound):
        await view_case(
            ViewCaseQuery(
                tenant_id=OTHER_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                case_id=case.id,
            ),
            case_repo=repo,
        )


# ---------------------------------------------------------------------------
# 4. Missing case
# ---------------------------------------------------------------------------


async def test_view_case__missing_case_id__raises_notfound(
    seeded_case: tuple[InMemoryCaseRepository, Case],
) -> None:
    """A non-existent case id in the same tenant raises NotFound."""
    repo, _ = seeded_case
    with pytest.raises(NotFound):
        await view_case(
            ViewCaseQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                case_id=uuid4(),
            ),
            case_repo=repo,
        )


# ---------------------------------------------------------------------------
# 5. Sensitive redaction (FR-002 acceptance scenario A4)
# ---------------------------------------------------------------------------


async def test_build_summary__sensitive_case__redacts_structured_data() -> None:
    """A sensitive case's structured ``data`` is replaced with a redacted
    marker so the client cannot see the underlying content.
    """
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type, version = make_case_type(tenant_id=ACME_TENANT)
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Sensitive matter",
        description="",
        data={"title": "Sensitive matter", "priority": "urgent"},
        priority="urgent",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )

    summary = build_summary(case=case, case_type_key=case_type.key, sensitive=True)
    wire = summary_to_dict(summary)
    # Structured data must NOT leak through.
    assert wire["data"] == {"redacted": True, "reason": "sensitive"}
    # The reference + type are still visible — the operator must be able
    # to confirm the case exists (AGENTS.md §4.R12 / FR-002).
    assert wire["reference"] == "C-ACME-2026-000001"
    assert wire["case_type_key"] == case_type.key


async def test_build_summary__non_sensitive_case__keeps_structured_data() -> None:
    """Non-sensitive cases pass through with the original payload."""
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type, version = make_case_type(tenant_id=ACME_TENANT)
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000002"),
        case_type_version=version,
        title="Help with onboarding",
        description="",
        data={"title": "Help with onboarding", "priority": "normal"},
        priority="normal",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )

    summary = build_summary(case=case, case_type_key=case_type.key)
    wire = summary_to_dict(summary)
    assert wire["data"] == {
        "title": "Help with onboarding",
        "priority": "normal",
    }
    assert wire["reference"] == "C-ACME-2026-000002"


# ---------------------------------------------------------------------------
# 6. Workspace slot shape
# ---------------------------------------------------------------------------


async def test_build_summary__workspace_slots_are_redacted_pending_pillars() -> None:
    """Assignee / queue / SLA are returned as ``RedactedField`` markers
    until the corresponding pillars (P02 tasks, P09 SLA) land. The list
    view strips these slots entirely so the wire payload stays compact.
    """
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type, version = make_case_type(tenant_id=ACME_TENANT)
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000003"),
        case_type_version=version,
        title="Workspace slots test",
        description="",
        data={"title": "Workspace slots test", "priority": "normal"},
        priority="normal",
        created_by=INTAKE_USER,
        now=FIXED_NOW,
    )

    summary = build_summary(case=case, case_type_key=case_type.key)
    wire = summary_to_dict(summary)
    assert wire["assignee"] == {"redacted": True, "reason": "not_implemented"}
    assert wire["queue"] == {"redacted": True, "reason": "not_implemented"}
    assert wire["sla_state"] == {"redacted": True, "reason": "not_implemented"}
    assert wire["participants"] == []
    assert wire["open_tasks"] == []
    assert wire["recent_documents"] == []
    assert wire["recent_decisions"] == []
    assert wire["timeline"] == []

    # The list view strips these slots entirely.
    list_wire = summary_to_list_dict(summary)
    for slot in (
        "assignee",
        "queue",
        "sla_state",
        "participants",
        "open_tasks",
        "recent_documents",
        "recent_decisions",
        "timeline",
    ):
        assert slot not in list_wire


# ---------------------------------------------------------------------------
# 7. Pagination clamp
# ---------------------------------------------------------------------------


async def test_list_cases__limit_clamped_to_max_page_limit(
    seeded_case: tuple[InMemoryCaseRepository, Case],
) -> None:
    """The list use case clamps ``limit`` to ``MAX_PAGE_LIMIT`` (200)."""
    from app.application.cases.list_cases import ListCasesQuery  # noqa: PLC0415
    from app.application.cases.list_cases import handle as list_cases  # noqa: PLC0415

    repo, _ = seeded_case
    # The clamp is internal — pass an out-of-range value and assert the
    # repository sees the clamped limit. The fake returns ``[]`` when
    # nothing matches; we verify the clamp by inspecting the repository
    # call (the fake records last_seen_limit via a wrapper).
    seen: dict[str, int] = {}

    class _Recorder:
        def __init__(self, inner: CaseRepository) -> None:
            self._inner = inner

        async def list(
            self,
            tenant_id: UUID,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> Any:
            seen["limit"] = limit
            seen["offset"] = offset
            return await self._inner.list(tenant_id, limit=limit, offset=offset)

        async def add(self, case: Case) -> None:
            await self._inner.add(case)

        async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
            return await self._inner.get(tenant_id, case_id)

        async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
            return await self._inner.get_by_reference(tenant_id, reference)

        async def next_reference(
            self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
        ) -> CaseReference:
            return await self._inner.next_reference(tenant_id, now=now, type_key=type_key)

    await list_cases(
        ListCasesQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
            limit=10_000,
        ),
        case_repo=_Recorder(repo),
    )
    assert seen["limit"] == MAX_PAGE_LIMIT
    assert seen["limit"] == 200


async def test_list_cases__default_limit_is_50() -> None:
    """A query without an explicit ``limit`` uses the 50 default."""
    from app.application.cases.list_cases import ListCasesQuery  # noqa: PLC0415
    from app.application.cases.list_cases import handle as list_cases  # noqa: PLC0415

    repo = InMemoryCaseRepository()
    seen: dict[str, int] = {}

    class _Recorder:
        def __init__(self, inner: CaseRepository) -> None:
            self._inner = inner

        async def list(
            self,
            tenant_id: UUID,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> Any:
            seen["limit"] = limit
            seen["offset"] = offset
            return await self._inner.list(tenant_id, limit=limit, offset=offset)

        async def add(self, case: Case) -> None:
            await self._inner.add(case)

        async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
            return await self._inner.get(tenant_id, case_id)

        async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
            return await self._inner.get_by_reference(tenant_id, reference)

        async def next_reference(
            self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
        ) -> CaseReference:
            return await self._inner.next_reference(tenant_id, now=now, type_key=type_key)

    await list_cases(
        ListCasesQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("intake",),
        ),
        case_repo=_Recorder(repo),
    )
    assert seen["limit"] == 50
    assert seen["offset"] == 0


async def test_list_cases__actor_without_any_role__raises_forbidden() -> None:
    """A role-less actor is denied even when there is nothing to list."""
    from app.application.cases.list_cases import ListCasesQuery  # noqa: PLC0415
    from app.application.cases.list_cases import handle as list_cases  # noqa: PLC0415

    repo = InMemoryCaseRepository()
    with pytest.raises(Forbidden):
        await list_cases(
            ListCasesQuery(
                tenant_id=ACME_TENANT,
                actor_id=GHOST_USER,
                actor_roles=(),
            ),
            case_repo=repo,
        )


# ---------------------------------------------------------------------------
# Redacted marker
# ---------------------------------------------------------------------------


def test_redacted_to_dict__serializes_to_wire_shape() -> None:
    """``Redacted.to_dict`` produces the on-wire ``{redacted, reason}``."""
    assert _redacted("sensitive").to_dict() == {"redacted": True, "reason": "sensitive"}
    assert _redacted("not_implemented").to_dict() == {
        "redacted": True,
        "reason": "not_implemented",
    }
    # The type itself is frozen so a misuse (mutating the reason) is a
    # clear runtime error.
    with pytest.raises(Exception, match="cannot assign"):
        _redacted("x").reason = "y"  # type: ignore[misc]


def test_redacted_is_redacted_type() -> None:
    """The summary module exports ``Redacted`` so callers can type-narrow."""
    assert isinstance(_redacted(), Redacted)
