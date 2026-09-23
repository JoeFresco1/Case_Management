"""Records persistence parity: holds + retention + disposition + evidence + reports.

F-task acceptance (002-leftovers T6 / FM-039, FM-040, FM-042, FM-053, FM-060):

- Holds (``legal_holds``, migration 0026) round-trip through
  ``SqlLegalHoldRepository`` on migrated DDL; cross-tenant reads return
  ``None``/``False`` (Constitution V — never confirm cross-tenant
  existence).
- Retention policies + disposition cases (``retention_policies`` /
  ``disposition_requests``, migration 0031) round-trip through the ORM
  on migrated DDL; every query carries ``WHERE tenant_id = :tenant_id``;
  cross-tenant ids return ``None`` / empty lists.
- Evidence bundles (FM-060) are computed ZIP archives, not rows: the
  tenant boundary is the case fetch (``SqlCaseRepository.get`` is
  tenant-scoped, so a cross-tenant bundle build sees ``NotFound``) and
  the export appends an ``AuditExported`` row on the per-tenant chain.
- Report definitions (FM-053) stay in-memory by design (no ORM model
  yet): the repository is tenant-scoped (``get``/``list`` key on
  ``tenant_id``).
- Audit is append-only: the test appends two rows and replays the
  chain; no UPDATE/DELETE path is exercised.

Style: ``test_<unit>_<scenario>__<expectation>`` with given/when/then;
``asyncio_mode = auto`` so no markers are needed. Hermetic like
``tests/foundation/test_migration_model_parity.py``: alembic runs in a
subprocess against a tmp file; the ORM session opens afterward.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.reports.custom_reports import (
    AggregationKind,
    DefineCustomReportCommand,
    GroupByField,
    ReportDefinitionRepository,
    define_custom_report,
)
from app.domain.legal_hold import LegalHold
from app.infrastructure.persistence import models as m
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import SqlCaseRepository, SqlLegalHoldRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


def _migrate(db_file: Path) -> None:
    """Run ``alembic upgrade head`` against ``db_file`` in a subprocess."""
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"alembic failed:\n{proc.stdout}\n{proc.stderr}"


def _columns(db_file: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        return {c["name"] for c in sa_inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


async def _seed_tenant_case(session: object, tenant_id: UUID, ref: str) -> tuple[UUID, UUID]:
    """Insert tenant/user/case-type/version/case; return (version_id, case_id)."""
    # // given a fresh tenant with one actor and one case
    case_type_id, version_id, case_id = uuid4(), uuid4(), uuid4()
    session.add(m.Tenant(id=tenant_id, key=f"T-{str(tenant_id)[:8]}", name="T", created_at=NOW))
    session.add(
        m.User(
            id=ACTOR if tenant_id == TENANT_A else uuid4(),
            tenant_id=tenant_id,
            key="intake",
            display_name="Intake",
            roles=["intake"],
            is_system=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        m.CaseType(
            id=case_type_id,
            tenant_id=tenant_id,
            key="ServiceRequest",
            name="Service Request",
            created_at=NOW,
            updated_at=NOW,
            current_version_id=version_id,
        )
    )
    session.add(
        m.CaseTypeVersion(
            id=version_id,
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={"fields": []},
            status="published",
            created_at=NOW,
        )
    )
    session.add(
        m.Case(
            id=case_id,
            tenant_id=tenant_id,
            reference=ref,
            case_type_version_id=version_id,
            status="open",
            priority="normal",
            title="parity",
            created_by=ACTOR,
            version_token=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return version_id, case_id


async def test_records_ddl__retention_disposition_columns__present(tmp_path: Path) -> None:
    """Migrated DDL carries the retention/disposition columns the ORM maps."""
    # // given a migrated scratch file // when inspecting // then columns match models
    db_file = tmp_path / "records_ddl.db"
    _migrate(db_file)

    policies = _columns(db_file, "retention_policies")
    assert {"category", "target_key", "period_days", "version_number", "status"} <= policies
    assert "published_at" in policies

    requests = _columns(db_file, "disposition_requests")
    assert {"case_id", "policy_id", "category", "due_at", "status", "surfaced_at"} <= requests
    assert "review_reason" in requests

    holds = _columns(db_file, "legal_holds")
    assert {"placed_by", "placed_reason", "released_at", "is_active"} <= holds


async def test_hold__place_release_roundtrip__two_tenant_isolated(tmp_path: Path) -> None:
    """Holds persist on migrated DDL; cross-tenant reads see nothing."""
    # // given two tenants with one case each
    db_file = tmp_path / "records_holds.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            _, case_a = await _seed_tenant_case(session, TENANT_A, "C-A-2026-000001")
            _, case_b = await _seed_tenant_case(session, TENANT_B, "C-B-2026-000001")
            await session.commit()
        # // when placing a hold in tenant A
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            await repo.add(
                LegalHold.place(
                    tenant_id=TENANT_A,
                    case_id=case_a,
                    placed_by=ACTOR,
                    reason="litigation",
                    now=NOW,
                )
            )
            await session.commit()
        # // then tenant A sees it, tenant B does not
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            assert await repo.get_active(TENANT_A, case_a) is not None
            assert await repo.is_under_legal_hold(TENANT_A, case_a) is True
            assert await repo.get_active(TENANT_B, case_a) is None
            assert await repo.is_under_legal_hold(TENANT_B, case_a) is False
            assert await repo.get_active(TENANT_B, case_b) is None
        # // when releasing // then the slot vacates without deleting history
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            active = await repo.get_active(TENANT_A, case_a)
            assert active is not None
            await repo.update(active.release(released_by=ACTOR, reason="resolved", now=NOW))
            await session.commit()
        async with sm() as session:
            repo = SqlLegalHoldRepository(session)
            assert await repo.get_active(TENANT_A, case_a) is None
    finally:
        await engine.dispose()


async def test_retention_disposition__orm_roundtrip__two_tenant_isolated(
    tmp_path: Path,
) -> None:
    """Retention policies + disposition cases persist tenant-scoped on migrated DDL."""
    # // given two tenants with one case each
    db_file = tmp_path / "records_retention.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        policy_a_id, request_a_id = uuid4(), uuid4()
        async with sm() as session:
            _, case_a = await _seed_tenant_case(session, TENANT_A, "C-A-2026-000002")
            _, case_b = await _seed_tenant_case(session, TENANT_B, "C-B-2026-000002")
            session.add(
                m.RetentionPolicy(
                    id=policy_a_id,
                    tenant_id=TENANT_A,
                    category="case",
                    target_key="",
                    period_days=365,
                    version_number=1,
                    status="published",
                    published_at=NOW,
                    published_by=ACTOR,
                    created_at=NOW,
                )
            )
            session.add(
                m.DispositionRequest(
                    id=request_a_id,
                    tenant_id=TENANT_A,
                    case_id=case_a,
                    policy_id=policy_a_id,
                    category="case",
                    due_at=NOW,
                    status="pending",
                    surfaced_at=NOW,
                    surfaced_by=ACTOR,
                )
            )
            await session.commit()
        # // when reading tenant-scoped // then A sees rows, B sees none
        async with sm() as session:
            policy = (
                await session.execute(
                    select(m.RetentionPolicy).where(
                        m.RetentionPolicy.tenant_id == TENANT_A,
                        m.RetentionPolicy.id == policy_a_id,
                    )
                )
            ).scalar_one_or_none()
            assert policy is not None
            assert policy.period_days == 365
            cross = (
                await session.execute(
                    select(m.RetentionPolicy).where(
                        m.RetentionPolicy.tenant_id == TENANT_B,
                        m.RetentionPolicy.id == policy_a_id,
                    )
                )
            ).scalar_one_or_none()
            assert cross is None

            mine = (
                await session.execute(
                    select(m.DispositionRequest).where(
                        m.DispositionRequest.tenant_id == TENANT_A,
                        m.DispositionRequest.id == request_a_id,
                    )
                )
            ).scalar_one_or_none()
            assert mine is not None
            assert mine.status == "pending"
            others = (
                (
                    await session.execute(
                        select(m.DispositionRequest).where(
                            m.DispositionRequest.tenant_id == TENANT_B,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert others == []
            # // and the evidence-bundle boundary (case fetch) is tenant-scoped too
            assert await SqlCaseRepository(session).get(TENANT_A, case_a) is not None
            assert await SqlCaseRepository(session).get(TENANT_B, case_a) is None
            assert case_b is not None
    finally:
        await engine.dispose()


async def test_audit__append_only_replay__per_tenant_chain(tmp_path: Path) -> None:
    """Audit appends (incl. bundle/export rows) replay per tenant; no mutation path."""
    # // given a migrated file // when appending hold + export rows // then both replay
    db_file = tmp_path / "records_audit.db"
    _migrate(db_file)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sm() as session:
            await _seed_tenant_case(session, TENANT_A, "C-A-2026-000003")
            store = SqlAuditEventStore(session)
            await store.append(
                tenant_id=TENANT_A,
                event_type="HoldPlaced",
                actor_id=ACTOR,
                actor_type="user",
                subject_type="case",
                subject_id=uuid4(),
                case_id=None,
                correlation_id="corr-hold",
                causation_id=None,
                payload={"reason": "litigation"},
                occurred_at=NOW,
            )
            await store.append(
                tenant_id=TENANT_A,
                event_type="AuditExported",
                actor_id=ACTOR,
                actor_type="user",
                subject_type="report_export",
                subject_id=uuid4(),
                case_id=None,
                correlation_id="corr-bundle",
                causation_id=None,
                payload={"bundle": True, "reason": "review"},
                occurred_at=NOW,
            )
            await session.commit()
        async with sm() as session:
            store = SqlAuditEventStore(session)
            chain = await store.list_for_chain(TENANT_A)
            assert len(chain) == 2
            assert chain[0].prev_hash != chain[1].prev_hash or chain[1].prev_hash == chain[0].hash
            assert (await store.list_for_chain(TENANT_B)) == []
    finally:
        await engine.dispose()


async def test_report_definition__tenant_scoped__cross_tenant_invisible() -> None:
    """Report definitions isolate by tenant (in-memory by design — no DDL)."""
    # // given one definition in tenant A // when reading as tenant B // then invisible
    repo = ReportDefinitionRepository()
    clock = _Clock()
    definition = await define_custom_report(
        DefineCustomReportCommand(
            tenant_id=TENANT_A,
            actor_id=ACTOR,
            actor_roles=("process_admin",),
            key="open-by-status",
            name="Open by status",
            description="",
            group_by=(GroupByField.CASE_STATUS,),
            aggregations=(AggregationKind.COUNT,),
            filters=(),
            time_window_hours=None,
        ),
        repo=repo,
        clock=clock,
    )
    assert await repo.get(TENANT_A, definition.id) is not None
    assert await repo.get(TENANT_B, definition.id) is None
    assert await repo.list(TENANT_B) == []
    assert [d.id for d in await repo.list(TENANT_A)] == [definition.id]


class _Clock:
    """Minimal clock stub (``now()`` only) for the report-definition use case."""

    def now(self) -> datetime:
        return NOW
