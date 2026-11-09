"""Interface contracts for records, evidence & reporting (T6).

Covers the HTTP boundary in ``app.interfaces.http.routers.holds``,
``app.interfaces.http.routers.retention``,
``app.interfaces.http.routers.retention_evidence`` and
``app.interfaces.http.routers.reports``:

- mutations without ``Idempotency-Key`` are 400 (router-level contract,
  deterministic without the middleware);
- authorized mutations return the canonical wire shape (201 for creates);
- double-place / decide-while-decided surface as 409 ``conflict``
  (optimistic-concurrency contract);
- non-admin actors are 403 with the ``ErrorResponse`` envelope;
- cross-tenant case ids are 404 without leaking foreign existence;
- evidence + operational reports + dashboards carry ``generated_at``
  source-time (+ ``team`` scope / policy version) for the UI;
- custom-definition validation failures are 422.

Naming follows AGENTS.md R5: ``test_<unit>_<scenario>__<expectation>``.
Structure is ``// given / // when / // then``. The suite is
sqlite-backed so it never requires Docker (AGENTS.md R1 fallback).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.reports import custom_reports
from app.deps import UserContext, get_current_user, get_db
from app.domain.case_type import CaseTypeField, CaseTypeSchema, CaseTypeVersionStatus
from app.domain.errors import DomainError
from app.infrastructure.persistence import models  # noqa: F401 — register tables
from app.infrastructure.persistence.hold_http_dependencies import (
    SqlHoldHttpDependencyProvider,
)
from app.infrastructure.persistence.legal_hold_repository import (
    InMemoryLegalHoldRepository,
)
from app.infrastructure.persistence.reports_http_dependencies import (
    SqlReportsHttpDependencyProvider,
)
from app.infrastructure.persistence.retention_http_dependencies import (
    SqlRetentionHttpDependencyProvider,
)
from app.infrastructure.persistence.retention_repository import (
    InMemoryCaseArchivalRepository,
    InMemoryDisposalFreezeRepository,
    InMemoryDispositionRequestRepository,
    InMemoryOrphanCaseProposalRepository,
    InMemoryRetentionPolicyRepository,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.holds import router as holds_router
from app.interfaces.http.routers.reports import router as reports_router
from app.interfaces.http.routers.retention import router as retention_router
from app.interfaces.http.routers.retention_evidence import (
    router as retention_evidence_router,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
GHOST = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
MISSING_CASE_ID = UUID("77777777-7777-7777-7777-777777777777")


def _utc(year: int, month: int, day: int) -> datetime:
    """Return a fixed UTC instant for deterministic seeding."""
    return datetime(year, month, day, 12, 0, 0, tzinfo=UTC)


FIXED_NOW = _utc(2026, 8, 31)


def _run(coro: Any) -> Any:
    """Run an awaitable to completion in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_tenant_and_type(
    session: AsyncSession, *, tenant_id: UUID, key: str = "ServiceRequest"
) -> UUID:
    """Insert a tenant plus a published case type; return the version id."""
    from app.infrastructure.persistence.models import CaseType as CaseTypeRow  # noqa: PLC0415
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseTypeVersion as CaseTypeVersionRow,
    )
    from app.infrastructure.persistence.models import Tenant as TenantRow  # noqa: PLC0415

    session.add(
        TenantRow(
            id=tenant_id,
            key=f"T{tenant_id.hex[:6]}",
            name="Synthetic",
            created_at=FIXED_NOW,
        )
    )
    case_type_id = uuid4()
    version_id = uuid4()
    session.add(
        CaseTypeRow(
            id=case_type_id,
            tenant_id=tenant_id,
            key=key,
            name=key,
            description="",
            sensitivity_default="internal",
            current_version_id=version_id,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
    )
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title", label="Title", type="text", required=True, sensitivity="internal"
            ),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high", "urgent"),
                sensitivity="internal",
            ),
        )
    )
    session.add(
        CaseTypeVersionRow(
            id=version_id,
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "enum_values": list(f.enum_values),
                        "sensitivity": f.sensitivity,
                        "searchable": f.searchable,
                    }
                    for f in schema.fields
                ]
            },
            document_classifications=[],
            rules=[],
            status=CaseTypeVersionStatus.PUBLISHED.value,
            published_at=FIXED_NOW,
            published_by=ADMIN_A,
            created_at=FIXED_NOW,
        )
    )
    await session.flush()
    return version_id


async def _seed_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_type_version_id: UUID,
    reference: str = "C-TEST-2026-000001",
) -> UUID:
    """Insert one open case; return its id."""
    from app.infrastructure.persistence.models import Case as CaseRow  # noqa: PLC0415

    case_id = uuid4()
    session.add(
        CaseRow(
            id=case_id,
            tenant_id=tenant_id,
            reference=reference,
            case_type_version_id=case_type_version_id,
            status="open",
            priority="normal",
            title="Synthetic retention candidate",
            description="",
            data={"title": "Synthetic retention candidate", "priority": "normal"},
            current_workflow_state="intake",
            version_token=1,
            created_by=ADMIN_A,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
            closed_at=None,
        )
    )
    await session.flush()
    return case_id


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("process_admin",),
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app with production SQL providers + in-memory records stores."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.hold_http_dependencies = SqlHoldHttpDependencyProvider()
    app.state.retention_http_dependencies = SqlRetentionHttpDependencyProvider()
    app.state.reports_http_dependencies = SqlReportsHttpDependencyProvider()
    # Records stores are process-scoped (AGENTS.md §12 seam placeholders).
    app.state.legal_hold_repository = InMemoryLegalHoldRepository()
    app.state.retention_policy_repository = InMemoryRetentionPolicyRepository()
    app.state.disposition_repository = InMemoryDispositionRequestRepository()
    app.state.case_archival_repository = InMemoryCaseArchivalRepository()
    app.state.disposal_freeze_repository = InMemoryDisposalFreezeRepository()
    app.state.orphan_proposal_repository = InMemoryOrphanCaseProposalRepository()
    # No IdempotencyMiddleware here: router-level Idempotency-Key checks are
    # the contract under test (deterministic without transport replay).
    app.include_router(holds_router, prefix="/api/v1")
    app.include_router(retention_router, prefix="/api/v1")
    app.include_router(retention_evidence_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    return app


def _seed_case_in_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: UUID = TENANT_A
) -> UUID:
    """Seed tenant + type + case; return the case id."""

    async def _seed() -> UUID:
        async with session_factory() as session:
            version_id = await _seed_tenant_and_type(session, tenant_id=tenant_id)
            case_id = await _seed_case(
                session, tenant_id=tenant_id, case_type_version_id=version_id
            )
            await session.commit()
            return case_id

    return _run(_seed())


# ---------------------------------------------------------------------------
# Hold lifecycle: idempotency + auth + concurrency contracts
# ---------------------------------------------------------------------------


def test_hold_place__missing_idempotency_key__returns_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /cases/{id}/holds without a key is 400 before any DB touch."""
    # // given a seeded case
    case_id = _seed_case_in_tenant(sql_session_factory)
    app = _build_app(sql_session_factory)

    # // when the key is omitted
    with TestClient(app) as client:
        response = client.post(f"/api/v1/cases/{case_id}/holds", json={"reason": "litigation"})

    # // then the router-level contract rejects the call
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Idempotency-Key header is required"


def test_hold_place__actor_without_hold_role__returns_403(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An actor without the hold role is denied with the ErrorResponse envelope."""
    # // given a seeded case + a role-less actor
    case_id = _seed_case_in_tenant(sql_session_factory)
    app = _build_app(sql_session_factory, roles=(), user_id=GHOST)

    # // when placing a hold with a key
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/cases/{case_id}/holds",
            json={"reason": "litigation"},
            headers={"Idempotency-Key": "hold-key-1"},
        )

    # // then the deny-by-default policy forbids the call
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_hold_place__double_place__returns_409_conflict(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Placing twice is a 409 conflict (concurrency contract)."""
    # // given a seeded case
    case_id = _seed_case_in_tenant(sql_session_factory)
    app = _build_app(sql_session_factory)

    # // when placing, then placing again with a fresh key
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/cases/{case_id}/holds",
            json={"reason": "litigation"},
            headers={"Idempotency-Key": "hold-key-1"},
        )
        assert first.status_code == 201, first.text
        assert first.json()["is_active"] is True

        second = client.post(
            f"/api/v1/cases/{case_id}/holds",
            json={"reason": "litigation again"},
            headers={"Idempotency-Key": "hold-key-2"},
        )

    # // then the second placement conflicts instead of duplicating
    assert second.status_code == 409, second.text


def test_hold_release__no_active_hold__returns_409(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Releasing without an active hold is a 409 conflict."""
    # // given a seeded case with no hold
    case_id = _seed_case_in_tenant(sql_session_factory)
    app = _build_app(sql_session_factory)

    # // when releasing with a key (DELETE with JSON body)
    with TestClient(app, follow_redirects=True) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/cases/{case_id}/holds",
            json={"reason": "matter closed"},
            headers={"Idempotency-Key": "release-key-1"},
        )

    # // then the use case refuses the release as a conflict
    assert response.status_code == 409, response.text


def test_hold_place__cross_tenant_case__returns_404(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A case in TENANT_A placed from TENANT_B surfaces as 404."""
    # // given a case in TENANT_A
    case_id = _seed_case_in_tenant(sql_session_factory, tenant_id=TENANT_A)
    app = _build_app(sql_session_factory, tenant_id=TENANT_B)

    # // when placing from the foreign tenant
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/cases/{case_id}/holds",
            json={"reason": "litigation"},
            headers={"Idempotency-Key": "hold-key-x"},
        )

    # // then existence is not confirmed across tenants
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Retention policy publish: idempotency + wire shape
# ---------------------------------------------------------------------------


def test_retention_publish__missing_idempotency_key__returns_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /admin/retention-policies without a key is 400."""
    # // given the records app
    app = _build_app(sql_session_factory)

    # // when publishing without a key
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/retention-policies",
            json={"category": "case", "target_key": "", "period_days": 365},
        )

    # // then the router-level contract rejects the call
    assert response.status_code == 400, response.text


def test_retention_publish__authorized__returns_201_with_version(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An authorized publish returns the versioned policy wire shape."""
    # // given the records app
    app = _build_app(sql_session_factory)

    # // when publishing a case policy
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/retention-policies",
            json={"category": "case", "target_key": "", "period_days": 365},
            headers={"Idempotency-Key": "policy-key-1"},
        )

    # // then the first version is published with its period + source ids
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version_number"] == 1
    assert body["period_days"] == 365
    assert body["status"] == "published"


def test_retention_publish__non_admin__returns_403(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A role-less actor cannot publish retention policy (403 envelope)."""
    # // given the records app with a role-less actor
    app = _build_app(sql_session_factory, roles=(), user_id=GHOST)

    # // when publishing with a key
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/retention-policies",
            json={"category": "case", "target_key": "", "period_days": 365},
            headers={"Idempotency-Key": "policy-key-2"},
        )

    # // then the call is forbidden with the structured envelope
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Evidence report: policy + source-time UI contract
# ---------------------------------------------------------------------------


def test_evidence_report__undisposed_case__returns_policy_source_time(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /retention/evidence/{id} carries kind/generated_at + disposed=false."""
    # // given a seeded undisposed case
    case_id = _seed_case_in_tenant(sql_session_factory)
    app = _build_app(sql_session_factory)

    # // when reading the chain-of-custody report
    with TestClient(app) as client:
        response = client.get(f"/api/v1/retention/evidence/{case_id}")

    # // then the snapshot declares its source-time + absence of disposition
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "snapshot"
    assert body["generated_at"] is not None
    assert body["disposed"] is False
    assert body["case_id"] == str(case_id)


def test_evidence_report__cross_tenant_case__returns_404(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A foreign-tenant evidence read is 404 (never confirms existence)."""
    # // given a case in TENANT_A
    case_id = _seed_case_in_tenant(sql_session_factory, tenant_id=TENANT_A)
    app = _build_app(sql_session_factory, tenant_id=TENANT_B)

    # // when reading from the foreign tenant
    with TestClient(app) as client:
        response = client.get(f"/api/v1/retention/evidence/{case_id}")

    # // then the report does not confirm the foreign case
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Operational reports + dashboard: source-time + team scope contract
# ---------------------------------------------------------------------------


def test_operational_reports__snapshots__carry_source_time_and_team(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Open-cases + overdue-tasks snapshots expose generated_at + team scope."""
    # // given the reports app (empty team)
    app = _build_app(sql_session_factory)

    # // when reading both snapshots
    with TestClient(app) as client:
        open_cases = client.get("/api/v1/reports/open-cases")
        overdue = client.get("/api/v1/reports/overdue-tasks")

    # // then each snapshot declares kind/source-time/scope for the UI
    assert open_cases.status_code == 200, open_cases.text
    assert overdue.status_code == 200, overdue.text
    for payload in (open_cases.json(), overdue.json()):
        assert payload["kind"] == "snapshot"
        assert payload["generated_at"] is not None
        assert isinstance(payload["team"], list)
        assert isinstance(payload["total"], int)


def test_operational_dashboard__returns_freshness_bound(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /reports/dashboards/operational returns KPIs + freshness <= 5s."""
    # // given the reports app
    app = _build_app(sql_session_factory)

    # // when reading the manager dashboard
    with TestClient(app) as client:
        response = client.get("/api/v1/reports/dashboards/operational")

    # // then KPIs + source-time + freshness window render for the UI
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "snapshot"
    assert body["generated_at"] is not None
    assert body["served_at"] is not None
    assert 0 <= body["freshness_seconds"] <= 5
    assert body["kpis"]["total_open_cases"] >= 0
    assert body["kpis"]["total_overdue_tasks"] >= 0


def test_reports__unauthorized_actor__returns_403_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A role-less actor sees 403 with the structured envelope on reports."""
    # // given the reports app with a role-less actor
    app = _build_app(sql_session_factory, roles=(), user_id=GHOST)

    # // when reading the open-cases snapshot
    with TestClient(app) as client:
        response = client.get("/api/v1/reports/open-cases")

    # // then the denial is structured (no stack/SQL leak)
    assert response.status_code == 403, response.text
    body = response.json()
    code = body.get("code", body.get("detail", {}).get("code"))
    assert code == "forbidden", body


# ---------------------------------------------------------------------------
# Custom definitions + export: idempotency + validation contracts
# ---------------------------------------------------------------------------


def test_custom_definition__missing_idempotency_key__returns_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /reports/custom-definitions without a key is 400."""
    # // given the reports app
    app = _build_app(sql_session_factory)

    # // when defining without a key
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/custom-definitions",
            json={
                "key": "open-by-status",
                "name": "Open by status",
                "description": "",
                "group_by": ["case.status"],
                "aggregations": ["count"],
                "filters": [],
                "time_window_hours": None,
            },
        )

    # // then the router-level contract rejects the call
    assert response.status_code == 400, response.text


def test_custom_definition__invalid_spec__returns_422(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A closed-vocabulary violation is 422 (governed report export)."""
    # // given the reports app
    custom_reports.reset_default_repository()
    app = _build_app(sql_session_factory)

    # // when defining with an unknown group-by field
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/custom-definitions",
            json={
                "key": "bad-spec",
                "name": "Bad spec",
                "description": "",
                "group_by": ["case.nonexistent_field"],
                "aggregations": ["count"],
                "filters": [],
                "time_window_hours": None,
            },
            headers={"Idempotency-Key": "custom-key-1"},
        )

    # // then the closed vocabulary rejects the spec as unprocessable
    assert response.status_code == 422, response.text
    custom_reports.reset_default_repository()


async def _seed_report_team(
    session_factory: async_sessionmaker[AsyncSession],
) -> UUID:
    """Create two tenants sharing a queue key and cases with distinct task histories."""
    visible_id = uuid4()
    async with session_factory() as session:
        for tenant in (TENANT_A, TENANT_B):
            version = await _seed_tenant_and_type(session, tenant_id=tenant)
            session.add(
                models.Queue(
                    tenant_id=tenant,
                    key="review",
                    name="Review",
                    member_user_ids=[str(ADMIN_A)],
                    created_at=FIXED_NOW,
                    updated_at=FIXED_NOW,
                )
            )
            for seq, (queue, statuses) in enumerate(
                (
                    ("review", ("open", "open")),
                    ("other", ("open",)),
                    ("review", ("completed",)),
                    ("review", ("open",)),
                ),
                start=1,
            ):
                case_id = await _seed_case(
                    session,
                    tenant_id=tenant,
                    case_type_version_id=version,
                    reference=f"C-TEST-2026-{seq:06d}",
                )
                case = await session.get(models.Case, case_id)
                assert case is not None
                case.updated_at = datetime.now(UTC)
                if seq == 4:
                    case.status = "closed"
                elif seq == 1 and tenant == TENANT_A:
                    visible_id = case_id
                for status in statuses:
                    session.add(
                        models.Task(
                            tenant_id=tenant,
                            case_id=case_id,
                            type="review",
                            title="Review",
                            status=status,
                            assignee_queue_id=queue,
                            due_at=FIXED_NOW,
                            created_at=FIXED_NOW,
                            updated_at=FIXED_NOW,
                        )
                    )
        await session.commit()
    return visible_id


def test_custom_report__live_sql_queue_scope__renders_only_matching_open_case(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate tasks, other queues, closed cases and other tenants cannot inflate a report."""
    # Given two tenants with the same review queue and overlapping task states.
    visible_id = _run(_seed_report_team(sql_session_factory))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # When the administrator renders a current report with a date window.
        definition = client.post(
            "/api/v1/reports/custom-definitions",
            json={
                "key": "live-sql-case-count",
                "name": "Case count",
                "group_by": ["case.status"],
                "aggregations": ["count"],
                "time_window_hours": 24,
            },
            headers={"Idempotency-Key": "live-sql-case-count"},
        )
        assert definition.status_code == 201, definition.text
        report = client.get(f"/api/v1/reports/custom/{definition.json()['id']}")
        cases = client.get("/api/v1/reports/open-cases")
        overdue = client.get("/api/v1/reports/overdue-tasks")
    # Then the case is counted once and the open overdue tasks use UTC deadlines.
    assert report.status_code == 200, report.text
    assert [row["count"] for row in report.json()["items"]] == [1]
    assert [row["case_id"] for row in cases.json()["items"]] == [str(visible_id)]
    assert overdue.status_code == 200, overdue.text
    assert all(row["overdue_seconds"] > 0 for row in overdue.json()["items"])
    assert overdue.json()["total"] == 3
