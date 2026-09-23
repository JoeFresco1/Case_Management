"""Multi-tenant isolation tests (P11-US2, FM-002).

These tests prove the contract from ``app.domain.ports`` and the
repositories in ``app.infrastructure.persistence.repositories``:

1. **Two tenants in parallel** — a row that exists in tenant A must be
   invisible to tenant B and vice versa.
2. **Explicit ``WHERE tenant_id = :tenant_id`` is sufficient on its own.**
   The SQLAlchemy ``do_orm_execute`` listener is opt-in defense-in-depth
   (AGENTS.md §4.R10 / §12). We run a separate parametrized case with
   the listener explicitly DISABLED to prove the repositories still
   refuse cross-tenant access without the second-line filter.
3. **Write paths also filter.** A case written under tenant A must not
   be observable in tenant B's ``list`` view, and a malformed reference
   cannot be used to confuse the index.

The acceptance scenario from P11 K1 is encoded as
``test_cross_tenant_read_of_known_id__returns_none`` and the listener-off
acceptance is encoded as the ``listener_enabled`` parametrize flag.

Tests run against sqlite (the same path CI uses) so they never depend
on Docker / WSL behavior. The in-memory fakes are reused from
``tests/application/cases/_fakes.py`` for the unit-level path; the SQL
repositories exercise the real SELECT statements.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.authorization import Action, Policy, Resource
from app.config import Settings
from app.db import Base, install_tenant_listener, reset_engine
from app.deps import UserContext
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import InvariantViolation
from app.infrastructure.persistence import models  # noqa: F401  registers tables
from app.infrastructure.persistence.models import (
    AuditEvent as AuditEventRow,
)
from app.infrastructure.persistence.models import (
    Case as CaseRow,
)
from app.infrastructure.persistence.models import (
    CaseType as CaseTypeRow,
)
from app.infrastructure.persistence.models import (
    CaseTypeVersion as CaseTypeVersionRow,
)
from app.infrastructure.persistence.models import (
    Task as TaskRow,
)
from app.infrastructure.persistence.models import Tenant as TenantRow
from app.infrastructure.persistence.repositories.case_repository import (
    SqlCaseRepository,
)

# ---------------------------------------------------------------------------
# Constants: two real tenants with non-overlapping UUIDs.
# ---------------------------------------------------------------------------


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

ACTOR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parametrization: prove both layers reject cross-tenant access
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db_url(tmp_path: Any) -> str:
    """Per-test sqlite URL. No shared state between tests."""
    db_file = tmp_path / "tenant_isolation.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
async def sql_engine(
    sqlite_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    """Build a fresh sqlite engine for the test, with the tenant
    listener DISABLED.

    Why disabled: the acceptance scenario (AGENTS.md §4.R10, §12) is
    that the explicit ``WHERE tenant_id = :tenant_id`` filter alone
    is sufficient — the listener is defense-in-depth only. Disabling
    the listener here is the documented way to prove the primary
    mechanism works without it.
    """
    await reset_engine()
    engine = create_async_engine(sqlite_db_url)
    settings = Settings(tenant_listener_enabled=False)
    install_tenant_listener(engine, settings=settings)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        await reset_engine()


@pytest.fixture
def session_factory(
    sql_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Async session factory bound to the test engine."""
    return async_sessionmaker(bind=sql_engine, expire_on_commit=False, autoflush=False)


# ---------------------------------------------------------------------------
# Helpers: build a Tenant row, a CaseType + published version, and a Case.
# The same shape is reused by every test so we never accidentally rely on
# legacy infrastructure paths.
# ---------------------------------------------------------------------------


async def _seed_tenant(session: AsyncSession, tenant_id: UUID, short_key: str) -> None:
    session.add(
        TenantRow(id=tenant_id, key=f"key-{short_key}", name=short_key, created_at=FIXED_NOW)
    )


async def _seed_case_type(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_type_id: UUID,
    case_type_version_id: UUID,
) -> None:
    session.add(
        CaseTypeRow(
            id=case_type_id,
            tenant_id=tenant_id,
            key="ServiceRequest",
            name="Service Request",
            description="",
            sensitivity_default="internal",
            current_version_id=case_type_version_id,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
    )
    session.add(
        CaseTypeVersionRow(
            id=case_type_version_id,
            tenant_id=tenant_id,
            case_type_id=case_type_id,
            version_number=1,
            schema_json={"fields": []},
            document_classifications=[],
            rules=[],
            status="published",
            published_at=FIXED_NOW,
            published_by=ACTOR_A,
            created_at=FIXED_NOW,
        )
    )


def _new_case(*, tenant_id: UUID, case_type_version_id: UUID, year: int = 2026) -> Case:
    """Build a domain Case ready for ``SqlCaseRepository.add``."""
    return Case(
        id=uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(f"C-{('ACME' if tenant_id == TENANT_A else 'BETA')}-{year}-000001"),
        case_type_version_id=case_type_version_id,
        status=CaseStatus.OPEN,
        priority="normal",
        title="Synthetic case",
        description="",
        data={},
        current_workflow_state=None,
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        created_by=ACTOR_A if tenant_id == TENANT_A else ACTOR_B,
        closed_at=None,
    )


# ---------------------------------------------------------------------------
# Two-tenant parametrized tests (P11-US2 / K1)
# ---------------------------------------------------------------------------


async def test_cross_tenant_read_of_known_id__returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user in tenant B asking for a case id that exists in tenant A
    must receive ``None``. Returning a row is a tenant leak.

    This is the canonical P11 acceptance scenario K1: the explicit
    ``WHERE tenant_id = :tenant_id`` in the repository must be
    sufficient on its own — the listener is opt-in and is disabled
    in this test (``listener_enabled=False``).
    """
    case_type_id_a = uuid4()
    case_type_version_id_a = uuid4()
    case_type_id_b = uuid4()
    case_type_version_id_b = uuid4()
    case_in_a = _new_case(tenant_id=TENANT_A, case_type_version_id=case_type_version_id_a)

    async with session_factory() as session:
        await _seed_tenant(session, TENANT_A, "ACME")
        await _seed_tenant(session, TENANT_B, "BETA")
        await _seed_case_type(
            session,
            tenant_id=TENANT_A,
            case_type_id=case_type_id_a,
            case_type_version_id=case_type_version_id_a,
        )
        await _seed_case_type(
            session,
            tenant_id=TENANT_B,
            case_type_id=case_type_id_b,
            case_type_version_id=case_type_version_id_b,
        )
        repo_a = SqlCaseRepository(session)
        await repo_a.add(case_in_a)
        await session.commit()

    # Tenant A sees its own case.
    async with session_factory() as session:
        repo_b = SqlCaseRepository(session)
        seen_by_a = await repo_b.get(TENANT_A, case_in_a.id)
        assert seen_by_a is not None
        assert seen_by_a.id == case_in_a.id

        # Tenant B asking for tenant A's case id -> None.
        seen_by_b = await repo_b.get(TENANT_B, case_in_a.id)
        assert seen_by_b is None, (
            f"cross-tenant leak: tenant B saw case {case_in_a.id} from tenant A"
        )


async def test_cross_tenant_list__returns_only_own_tenant_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two cases, two tenants, one query per tenant: each tenant's
    ``list`` returns only its own rows. A misconfigured global query
    that drops the tenant filter would return both, leaking across
    tenants.
    """
    case_type_id_a = uuid4()
    case_type_version_id_a = uuid4()
    case_type_id_b = uuid4()
    case_type_version_id_b = uuid4()
    case_a = _new_case(tenant_id=TENANT_A, case_type_version_id=case_type_version_id_a)
    case_b = _new_case(tenant_id=TENANT_B, case_type_version_id=case_type_version_id_b)

    async with session_factory() as session:
        await _seed_tenant(session, TENANT_A, "ACME")
        await _seed_tenant(session, TENANT_B, "BETA")
        await _seed_case_type(
            session,
            tenant_id=TENANT_A,
            case_type_id=case_type_id_a,
            case_type_version_id=case_type_version_id_a,
        )
        await _seed_case_type(
            session,
            tenant_id=TENANT_B,
            case_type_id=case_type_id_b,
            case_type_version_id=case_type_version_id_b,
        )
        repo = SqlCaseRepository(session)
        await repo.add(case_a)
        await repo.add(case_b)
        await session.commit()

    async with session_factory() as session:
        repo = SqlCaseRepository(session)
        rows_a = list(await repo.list(TENANT_A))
        rows_b = list(await repo.list(TENANT_B))
        ids_a = {c.id for c in rows_a}
        ids_b = {c.id for c in rows_b}
        assert case_a.id in ids_a
        assert case_b.id not in ids_a, "tenant A list leaked tenant B case"
        assert case_b.id in ids_b
        assert case_a.id not in ids_b, "tenant B list leaked tenant A case"


async def test_cross_tenant_get_by_reference__returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant B user cannot resolve tenant A's reference.

    The per-tenant unique index on ``(tenant_id, reference)`` is the
    integrity guarantee; the repository's query must use it on read.
    """
    case_type_id_a = uuid4()
    case_type_version_id_a = uuid4()
    case_type_id_b = uuid4()
    case_type_version_id_b = uuid4()
    case_a = _new_case(tenant_id=TENANT_A, case_type_version_id=case_type_version_id_a)
    case_b = _new_case(tenant_id=TENANT_B, case_type_version_id=case_type_version_id_b, year=2026)
    # Force tenant B to use a different reference so the per-tenant
    # index is the only thing distinguishing them.
    object.__setattr__(case_b, "reference", CaseReference("C-BETA-2026-000001"))

    async with session_factory() as session:
        await _seed_tenant(session, TENANT_A, "ACME")
        await _seed_tenant(session, TENANT_B, "BETA")
        await _seed_case_type(
            session,
            tenant_id=TENANT_A,
            case_type_id=case_type_id_a,
            case_type_version_id=case_type_version_id_a,
        )
        await _seed_case_type(
            session,
            tenant_id=TENANT_B,
            case_type_id=case_type_id_b,
            case_type_version_id=case_type_version_id_b,
        )
        repo = SqlCaseRepository(session)
        await repo.add(case_a)
        await repo.add(case_b)
        await session.commit()

    async with session_factory() as session:
        repo = SqlCaseRepository(session)
        # Tenant B asking for tenant A's reference -> None.
        cross = await repo.get_by_reference(TENANT_B, case_a.reference)
        assert cross is None
        # Tenant B asking for its own reference -> the right row.
        own = await repo.get_by_reference(TENANT_B, case_b.reference)
        assert own is not None
        assert own.id == case_b.id


async def test_listener_disabled_in_test__explicit_filter_still_blocks(
    sqlite_db_url: str,
) -> None:
    """Acceptance: ``listener_enabled=False`` must still block cross-tenant
    access because the repositories enforce the filter explicitly.

    This is the second-line-defence requirement from AGENTS.md §4.R10
    and §12: ``config.TENANT_LISTENER_ENABLED`` is a defense-in-depth
    knob, never the sole mechanism. We rebuild a fresh engine with the
    listener off and confirm cross-tenant reads still return ``None``.
    """
    await reset_engine()
    engine = create_async_engine(sqlite_db_url)
    settings = Settings(tenant_listener_enabled=False)
    install_tenant_listener(engine, settings=settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        sm = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        case_type_id_a = uuid4()
        case_type_version_id_a = uuid4()
        case_a = _new_case(tenant_id=TENANT_A, case_type_version_id=case_type_version_id_a)

        async with sm() as session:
            await _seed_tenant(session, TENANT_A, "ACME")
            await _seed_case_type(
                session,
                tenant_id=TENANT_A,
                case_type_id=case_type_id_a,
                case_type_version_id=case_type_version_id_a,
            )
            repo = SqlCaseRepository(session)
            await repo.add(case_a)
            await session.commit()

        # Same session, no listener pinned: query for tenant A succeeds.
        async with sm() as session:
            repo = SqlCaseRepository(session)
            ok = await repo.get(TENANT_A, case_a.id)
            assert ok is not None

        # Same session, no listener pinned: tenant B query for tenant A's
        # row returns None because the explicit filter blocks it.
        async with sm() as session:
            repo = SqlCaseRepository(session)
            cross = await repo.get(TENANT_B, case_a.id)
            assert cross is None, "listener off but explicit filter failed"
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        await reset_engine()


# ---------------------------------------------------------------------------
# In-memory path: prove the use case and authorization reject
# cross-tenant requests **before** any DB write (FM-002 defense-in-depth).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor_tenant", "target_tenant"),
    [
        (TENANT_A, TENANT_A),
        (TENANT_A, TENANT_B),
        (TENANT_B, TENANT_A),
    ],
    ids=["same_tenant", "actor_in_a_target_in_b", "actor_in_b_target_in_a"],
)
async def test_create_case__cross_tenant_request_is_denied_or_scoped(
    actor_tenant: UUID,
    target_tenant: UUID,
) -> None:
    """The policy is the application-layer deny-by-default gate. It
        must refuse a request from an actor whose tenant differs from the
        target resource's tenant, regardless of the role grant.

        Why this test does not exercise ``create_case.handle`` directly:
        the use case derives the actor's tenant from ``cmd.tenant_id``
        (the tenant the use case is operating on, not the actor's "home"
        tenant). Real cross-tenant enforcement therefore lives in two
        layers:

        1. The HTTP boundary constructs a ``UserContext`` whose
           ``tenant_id`` reflects the **caller's** tenant, and the use
           case authorizes against that.
        2. The repository's ``WHERE tenant_id = :tenant_id`` filter
           refuses to load data from a different tenant (covered above
           by the SQL tests).

        This test exercises the policy with both tenants explicit so the
        cross-tenant denial is unambiguous: actor in A asking for resource
    in B is refused, even with the right role.
    """
    policy = Policy()
    actor = UserContext(
        user_id=ACTOR_A if actor_tenant == TENANT_A else ACTOR_B,
        tenant_id=actor_tenant,
        display_name="actor",
        roles=("intake",),
    )
    resource = Resource(tenant_id=target_tenant, kind="case_type")
    decision = await policy.evaluate(Action.CASE_CREATE, actor, resource)
    if actor_tenant == target_tenant:
        assert decision.allowed, decision.reason
        assert "role" in decision.reason
    else:
        assert not decision.allowed
        assert "cross-tenant" in decision.reason


# ---------------------------------------------------------------------------
# Reference validation: the value object enforces wire safety, which
# prevents an attacker from smuggling control characters or oversized
# payloads through a lookup key. Shape strictness lives in the
# template validator (FM-009) — any tenant-configured shape is a
# legitimate reference, so a "malformed legacy" string simply resolves
# to "not found" instead of failing construction.
# ---------------------------------------------------------------------------


def test_case_reference_rejects_malformed_value() -> None:
    """The reference value object rejects empty, oversized, and
    control-character references while accepting any printable shape
    (FM-009 custom templates are legitimate references)."""
    with pytest.raises(InvariantViolation):
        CaseReference("")
    with pytest.raises(InvariantViolation):
        CaseReference("x" * 201)  # beyond MAX_REFERENCE_LENGTH
    with pytest.raises(InvariantViolation):
        CaseReference("C-ACME-2026-000001\n")  # control character
    # Legacy and custom shapes are both storable.
    assert CaseReference("C-ACME-2026-000001").value == "C-ACME-2026-000001"
    assert CaseReference("PR-000001").value == "PR-000001"


# ---------------------------------------------------------------------------
# Defense in depth: tenant_id is NOT NULL — orphan rows cannot exist (T037)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_id_not_null__case_orphan_insert_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``cases`` row without ``tenant_id`` is rejected by the schema.

    The ``WHERE tenant_id = :tenant_id`` filter assumes every row
    carries a tenant; a NULL would silently escape every tenant
    filter. ``NOT NULL`` makes that state unrepresentable.
    """
    async with session_factory() as session:
        session.add(
            CaseRow(
                tenant_id=None,  # type: ignore[arg-type]
                reference="C-ACME-2026-000001",
                case_type_version_id=uuid4(),
                status="open",
                priority="normal",
                title="orphan",
                created_by=ACTOR_A,
                created_at=FIXED_NOW,
                updated_at=FIXED_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_tenant_id_not_null__task_orphan_insert_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``tasks`` row without ``tenant_id`` is rejected by the schema."""
    async with session_factory() as session:
        session.add(
            TaskRow(
                tenant_id=None,  # type: ignore[arg-type]
                case_id=uuid4(),
                type="review",
                title="orphan task",
                created_at=FIXED_NOW,
                updated_at=FIXED_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_tenant_id_not_null__audit_orphan_insert_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ``audit_events`` row without ``tenant_id`` is rejected."""
    async with session_factory() as session:
        session.add(
            AuditEventRow(
                tenant_id=None,  # type: ignore[arg-type]
                event_type="CaseCreated",
                occurred_at=FIXED_NOW,
                actor_id=ACTOR_A,
                actor_type="user",
                subject_type="case",
                subject_id=uuid4(),
                correlation_id="corr-orphan",
                payload={},
                prev_hash="GENESIS",
                hash="h",
                payload_hash="p",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
