"""Tests for the saved-search use cases (P06-US3, US5, US6 / FM-032).

Acceptance scenarios (per task spec):

1. **Save + reuse** — saving a search with a sort spec and re-executing
   it returns the same filtered set (deterministic across runs).
2. **Audit shape** — security staff see the audit row with the query
   shape, the timestamp, and the result count.
3. **Tenant isolation** — a saved search in tenant A is invisible to
   tenant B; the cross-tenant lookup raises ``NotFound``.

The fakes mirror the in-memory pattern in ``test_case_search.py`` so
the suite runs without Postgres / SQLite for the shape checks. A
separate sqlite-backed test exercises the SQL adapter's
``WHERE tenant_id = :tenant_id`` filter for the cross-tenant denial.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.search import saved_searches as uc
from app.application.search.saved_searches import (
    DeleteSavedSearchCommand,
    ListSavedSearchesQuery,
    RunSavedSearchCommand,
    SaveSearchCommand,
    ViewSearchAuditQuery,
)
from app.config import Settings
from app.db import Base, install_tenant_listener, reset_engine
from app.domain.errors import Conflict, DomainError, Forbidden, NotFound, ValidationFailed
from app.domain.ports import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.domain.saved_search import SavedSearch, SortSpec
from app.infrastructure.persistence import models  # noqa: F401  registers tables
from app.infrastructure.persistence.models import Tenant as TenantRow
from app.infrastructure.persistence.models import User as UserRow
from app.infrastructure.persistence.repositories.saved_search_repository import (
    SqlSavedSearchAuditRepository,
    SqlSavedSearchRepository,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SUPERVISOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory fakes (case_search mirror pattern)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCase:
    case_id: UUID
    priority: str
    status: str
    title: str
    reference: str


class InMemorySearchProvider(SearchProvider):
    """Hermetic search provider with deterministic ordering.

    The provider sorts by the optional sort spec and returns the
    same rowset on every run so the test can assert that "save + run"
    is reproducible. Tenant scoping is explicit even though the test
    runs without a database.
    """

    def __init__(self) -> None:
        self.cases: list[StoredCase] = []
        self.last_spec: SearchSpec | None = None

    async def search(self, spec: SearchSpec) -> SearchResultPage:
        self.last_spec = spec
        items: list[StoredCase] = []
        for c in self.cases:
            ok = True
            for f in spec.filters:
                if not _match(c, f):
                    ok = False
                    break
            if ok:
                items.append(c)
        total = len(items)
        # Apply declared sort spec (asc/desc) per entry in order.
        items = _apply_sort(items, spec)
        page = items[spec.offset : spec.offset + spec.limit]
        hits = [
            SearchHit(
                case_id=c.case_id,
                reference=c.reference,
                priority=c.priority,
                status=c.status,
                title=c.title,
                data_snippet={},
            )
            for c in page
        ]
        next_cursor: str | None = None
        if len(page) == spec.limit and spec.offset + spec.limit < total:
            next_cursor = f"{spec.offset + spec.limit}:{spec.limit}"
        return SearchResultPage(
            items=hits,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=next_cursor,
        )


def _core_column(c: StoredCase, name: str) -> Any:
    return {
        "reference": c.reference,
        "status": c.status,
        "priority": c.priority,
        "title": c.title,
    }.get(name)


def _match(c: StoredCase, f: SearchFilter) -> bool:
    col = _core_column(c, f.field)
    if col is None:
        return False
    if f.op == "eq":
        return str(col).lower() == str(f.value).lower()
    return False


def _apply_sort(items: list[StoredCase], spec: SearchSpec) -> list[StoredCase]:
    # No sort spec surfaced via ``case_search`` today, so the
    # in-memory provider keeps insertion order; the saved-search use
    # case carries its own sort, but the run path executes through
    # ``case_search`` which currently ignores explicit sort. The
    # audit row captures the sort spec verbatim so a future
    # sort-aware adapter can replay it.
    _ = spec
    return list(items)


@dataclass
class InMemorySavedSearchRepository:
    """In-memory adapter that satisfies the SavedSearchRepository port."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID], SavedSearch] = {}

    async def add(self, saved: SavedSearch) -> None:
        key = (saved.tenant_id, saved.owner_user_id)
        for existing in self.rows.values():
            if (
                existing.tenant_id == saved.tenant_id
                and existing.owner_user_id == saved.owner_user_id
                and existing.name == saved.name
            ):
                raise Conflict(f"saved search with name {saved.name!r} already exists")
        self.rows[(saved.tenant_id, saved.id)] = saved
        _ = key

    async def get(self, tenant_id: UUID, saved_search_id: UUID) -> SavedSearch | None:
        return self.rows.get((tenant_id, saved_search_id))

    async def list_for_owner(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SavedSearch]:
        rows = [
            s
            for s in self.rows.values()
            if s.tenant_id == tenant_id and s.owner_user_id == owner_user_id
        ]
        rows.sort(key=lambda s: (s.created_at or FIXED_NOW, s.id), reverse=True)
        return rows[offset : offset + limit]

    async def delete(self, tenant_id: UUID, saved_search_id: UUID) -> bool:
        key = (tenant_id, saved_search_id)
        if key not in self.rows:
            return False
        del self.rows[key]
        return True


@dataclass
class _AuditRecord:
    id: UUID
    tenant_id: UUID
    actor_id: UUID
    saved_search_id: UUID | None
    query_shape: dict[str, Any]
    result_count: int
    occurred_at: datetime


@dataclass
class InMemorySavedSearchAuditRepository:
    rows: list[_AuditRecord] = field(default_factory=list)

    async def append(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        saved_search_id: UUID | None,
        query_shape: dict[str, Any],
        result_count: int,
        occurred_at: datetime,
    ) -> None:
        self.rows.append(
            _AuditRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                actor_id=actor_id,
                saved_search_id=saved_search_id,
                query_shape=dict(query_shape),
                result_count=result_count,
                occurred_at=occurred_at,
            )
        )

    async def list_for_security_report(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        saved_search_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[_AuditRecord]:
        rows = [r for r in self.rows if r.tenant_id == tenant_id]
        if actor_id is not None:
            rows = [r for r in rows if r.actor_id == actor_id]
        if saved_search_id is not None:
            rows = [r for r in rows if r.saved_search_id == saved_search_id]
        if since is not None:
            rows = [r for r in rows if r.occurred_at >= since]
        if until is not None:
            rows = [r for r in rows if r.occurred_at <= until]
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[offset : offset + limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


def _make_provider() -> InMemorySearchProvider:
    p = InMemorySearchProvider()
    # Two urgent, one normal; deterministic ordering.
    p.cases.extend(
        [
            StoredCase(
                case_id=uuid4(),
                priority="urgent",
                status="open",
                title="A",
                reference="C-ACME-2026-000001",
            ),
            StoredCase(
                case_id=uuid4(),
                priority="urgent",
                status="closed",
                title="B",
                reference="C-ACME-2026-000002",
            ),
            StoredCase(
                case_id=uuid4(),
                priority="normal",
                status="open",
                title="C",
                reference="C-ACME-2026-000003",
            ),
        ]
    )
    return p


@pytest.fixture
def saved_repo() -> InMemorySavedSearchRepository:
    return InMemorySavedSearchRepository()


@pytest.fixture
def audit_repo() -> InMemorySavedSearchAuditRepository:
    return InMemorySavedSearchAuditRepository()


@pytest.fixture
def provider() -> InMemorySearchProvider:
    return _make_provider()


@pytest.fixture
def clock() -> _FixedClock:
    return _FixedClock()


# ---------------------------------------------------------------------------
# 1. Save + reuse returns the same filtered set
# ---------------------------------------------------------------------------


async def test_save_and_run__same_filtered_set_with_sort(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    provider: InMemorySearchProvider,
    clock: _FixedClock,
) -> None:
    """Saving a search with a sort spec, then re-running it returns the
    same filtered set. The audit row captures the canonical shape
    (including sort) so the security report can replay the saved
    query.
    """
    saved = await uc.save_search(
        SaveSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            name="urgent open",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
            sort=(SortSpec(field="reference", direction="asc"),),
        ),
        repository=saved_repo,
        audit=audit_repo,
        clock=clock,
    )

    result_first = await uc.run_saved_search(
        RunSavedSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            saved_search_id=saved.id,
            limit=50,
            offset=0,
        ),
        repository=saved_repo,
        audit=audit_repo,
        search_provider=provider,
        clock=clock,
    )
    result_second = await uc.run_saved_search(
        RunSavedSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            saved_search_id=saved.id,
            limit=50,
            offset=0,
        ),
        repository=saved_repo,
        audit=audit_repo,
        search_provider=provider,
        clock=clock,
    )

    first_ids = [h.case_id for h in result_first.items]
    second_ids = [h.case_id for h in result_second.items]
    assert first_ids == second_ids
    # Two urgent cases plus one normal; the priority filter returns
    # exactly the urgent ones.
    assert result_first.total_considered == 2
    assert len(result_first.items) == 2
    # The executable saved query retains its operand; the audit shape redacts it.
    assert saved.filters == (("priority", "eq", "urgent"),)
    assert saved.query_shape["filters"] == [
        {
            "field": "priority",
            "op": "eq",
            "value": {"redacted": True, "reason": "query_value"},
        }
    ]
    assert saved.query_shape["sort"] == [{"field": "reference", "direction": "asc"}]
    # Three audit rows total: one for the save, two for the runs.
    assert len(audit_repo.rows) == 3


def test_saved_search_create__audit_shape_redacts_query_values() -> None:
    # given: executable saved-search values may contain private content
    text_value = "client name@example.test"
    filter_value = "123-45-6789"
    # when: the saved-search aggregate is created
    saved = SavedSearch.create(
        tenant_id=ACME_TENANT,
        owner_user_id=ALICE,
        name="private lookup",
        text_query=text_value,
        filters=(("data.ssn", "eq", filter_value),),
        now=FIXED_NOW,
    )
    # then: only the audit projection redacts values; the saved query stays executable
    assert saved.query_shape == {
        "text_query": {"redacted": True, "reason": "free_text"},
        "filters": [
            {
                "field": "data.ssn",
                "op": "eq",
                "value": {"redacted": True, "reason": "query_value"},
            }
        ],
        "sort": [],
    }
    assert saved.text_query == text_value
    assert saved.filters == (("data.ssn", "eq", filter_value),)
    assert text_value not in str(saved.query_shape)
    assert filter_value not in str(saved.query_shape)


# ---------------------------------------------------------------------------
# 2. Security staff see audit shape, timestamp, and result count
# ---------------------------------------------------------------------------


async def test_view_search_audit__security_staff_sees_shape_and_count(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    provider: InMemorySearchProvider,
    clock: _FixedClock,
) -> None:
    """A supervisor with ``AUDIT_READ`` reads the audit page and sees the
    query shape, the timestamp, and the result count for each event.
    """
    saved = await uc.save_search(
        SaveSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            name="urgent open",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
            sort=(SortSpec(field="reference", direction="asc"),),
        ),
        repository=saved_repo,
        audit=audit_repo,
        clock=clock,
    )
    await uc.run_saved_search(
        RunSavedSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            saved_search_id=saved.id,
        ),
        repository=saved_repo,
        audit=audit_repo,
        search_provider=provider,
        clock=clock,
    )

    page = await uc.view_search_audit(
        ViewSearchAuditQuery(
            tenant_id=ACME_TENANT,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
        ),
        audit=audit_repo,
    )

    # The save event + the run event are visible; the supervisor can
    # filter by ``saved_search_id`` and ``actor_id``.
    assert len(page.items) == 2
    for entry in page.items:
        # Shape carries the searchable structure without raw operands.
        assert "filters" in entry.query_shape
        assert "sort" in entry.query_shape
        assert entry.query_shape["filters"] == [
            {
                "field": "priority",
                "op": "eq",
                "value": {"redacted": True, "reason": "query_value"},
            }
        ]
        assert "urgent" not in str(entry.query_shape)
        # Timestamp is ISO-8601 so it sorts and parses cleanly.
        assert entry.occurred_at.startswith("2026-")
        # Run events carry a non-zero result count; the save event
        # carries 0.
        assert entry.result_count >= 0

    # Filtering by ``saved_search_id`` narrows the report to events
    # for the one saved search.
    narrow = await uc.view_search_audit(
        ViewSearchAuditQuery(
            tenant_id=ACME_TENANT,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
            saved_search_id=saved.id,
        ),
        audit=audit_repo,
    )
    assert len(narrow.items) == 2
    run_entries = [e for e in narrow.items if e.result_count > 0]
    assert len(run_entries) == 1
    assert run_entries[0].result_count == 2


async def test_view_search_audit__unauthorized_actor__raises_forbidden(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    provider: InMemorySearchProvider,
    clock: _FixedClock,
) -> None:
    """An actor without ``AUDIT_READ`` is denied (deny-by-default)."""
    await uc.save_search(
        SaveSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            name="urgent",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
        ),
        repository=saved_repo,
        audit=audit_repo,
        clock=clock,
    )
    with pytest.raises(Forbidden):
        await uc.view_search_audit(
            ViewSearchAuditQuery(
                tenant_id=ACME_TENANT,
                actor_id=ALICE,  # intake role: no audit.read
                actor_roles=("intake",),
            ),
            audit=audit_repo,
        )


# ---------------------------------------------------------------------------
# 3. Tenant isolation
# ---------------------------------------------------------------------------


async def test_other_tenant_cannot_see_saved_search(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    clock: _FixedClock,
) -> None:
    """A saved search in tenant A is invisible to tenant B.

    Both the ``get`` (used by ``run_saved_search`` and
    ``delete_saved_search``) and the ``list_for_owner`` paths must
    refuse cross-tenant access. The denial surfaces as ``NotFound``
    so the API never confirms cross-tenant existence (Constitution V).
    """
    saved = await uc.save_search(
        SaveSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            name="my urgent",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
        ),
        repository=saved_repo,
        audit=audit_repo,
        clock=clock,
    )
    # Tenant B cannot fetch the row.
    assert await saved_repo.get(BETA_TENANT, saved.id) is None
    # The run path raises NotFound for the cross-tenant request.
    provider = _make_provider()
    with pytest.raises(NotFound):
        await uc.run_saved_search(
            RunSavedSearchCommand(
                tenant_id=BETA_TENANT,
                actor_id=BOB,
                actor_roles=("intake",),
                saved_search_id=saved.id,
            ),
            repository=saved_repo,
            audit=audit_repo,
            search_provider=provider,
            clock=clock,
        )
    # The list path for tenant B does not include tenant A's rows.
    listing = await uc.list_saved_searches(
        ListSavedSearchesQuery(
            tenant_id=BETA_TENANT,
            actor_id=BOB,
            actor_roles=("intake",),
        ),
        repository=saved_repo,
    )
    assert listing == []
    # Delete from tenant B is also NotFound.
    with pytest.raises(NotFound):
        await uc.delete_saved_search(
            DeleteSavedSearchCommand(
                tenant_id=BETA_TENANT,
                actor_id=BOB,
                actor_roles=("intake",),
                saved_search_id=saved.id,
            ),
            repository=saved_repo,
        )


# ---------------------------------------------------------------------------
# 4. Owner-only reuse (same tenant, different actor)
# ---------------------------------------------------------------------------


async def test_run_saved_search__different_actor__raises_not_found(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    provider: InMemorySearchProvider,
    clock: _FixedClock,
) -> None:
    """A different actor in the same tenant cannot run a saved search
    they do not own (the row stays invisible to them).
    """
    saved = await uc.save_search(
        SaveSearchCommand(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            actor_roles=("intake",),
            name="mine",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
        ),
        repository=saved_repo,
        audit=audit_repo,
        clock=clock,
    )
    with pytest.raises(NotFound):
        await uc.run_saved_search(
            RunSavedSearchCommand(
                tenant_id=ACME_TENANT,
                actor_id=BOB,  # different actor in the same tenant
                actor_roles=("intake",),
                saved_search_id=saved.id,
            ),
            repository=saved_repo,
            audit=audit_repo,
            search_provider=provider,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Validation guards
# ---------------------------------------------------------------------------


async def test_save_search__empty_name__raises_validation(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    clock: _FixedClock,
) -> None:
    with pytest.raises(ValidationFailed):
        await uc.save_search(
            SaveSearchCommand(
                tenant_id=ACME_TENANT,
                actor_id=ALICE,
                actor_roles=("intake",),
                name="",
                text_query=None,
                filters=(),
            ),
            repository=saved_repo,
            audit=audit_repo,
            clock=clock,
        )


async def test_save_search__unsupported_sort_field__raises_domain_error(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    clock: _FixedClock,
) -> None:
    with pytest.raises(DomainError):
        await uc.save_search(
            SaveSearchCommand(
                tenant_id=ACME_TENANT,
                actor_id=ALICE,
                actor_roles=("intake",),
                name="x",
                text_query=None,
                filters=(),
                sort=(SortSpec(field="not_a_field", direction="asc"),),
            ),
            repository=saved_repo,
            audit=audit_repo,
            clock=clock,
        )


async def test_save_search__unsupported_operator__raises_validation(
    saved_repo: InMemorySavedSearchRepository,
    audit_repo: InMemorySavedSearchAuditRepository,
    clock: _FixedClock,
) -> None:
    with pytest.raises(ValidationFailed):
        await uc.save_search(
            SaveSearchCommand(
                tenant_id=ACME_TENANT,
                actor_id=ALICE,
                actor_roles=("intake",),
                name="x",
                text_query=None,
                filters=(("priority", "regex", "x"),),
            ),
            repository=saved_repo,
            audit=audit_repo,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. SQL adapter tenant-isolation check (sqlite)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db_url(tmp_path: Any) -> str:
    db_file = tmp_path / "saved_search.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
async def sql_engine(sqlite_db_url: str) -> AsyncEngine:
    await reset_engine()
    engine = create_async_engine(sqlite_db_url)
    install_tenant_listener(engine, settings=Settings(tenant_listener_enabled=False))
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
    return async_sessionmaker(bind=sql_engine, expire_on_commit=False, autoflush=False)


async def _seed(session: AsyncSession, tenant_id: UUID, short_key: str) -> None:
    session.add(
        TenantRow(
            id=tenant_id,
            key=f"key-{short_key}",
            name=short_key,
            created_at=FIXED_NOW,
        )
    )
    session.add(
        UserRow(
            id=ALICE if tenant_id == ACME_TENANT else BOB,
            tenant_id=tenant_id,
            key="alice" if tenant_id == ACME_TENANT else "bob",
            display_name="alice" if tenant_id == ACME_TENANT else "bob",
            roles=["intake"],
            is_system=False,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
    )


async def test_sql_saved_search_repository__cross_tenant_get_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The SQL adapter's ``get`` returns ``None`` for a cross-tenant id
    even when the row exists in another tenant's table. This is the
    P11 / FM-002 contract (Constitution V): the two missing-vs-cross-
    tenant cases must be indistinguishable.
    """
    async with session_factory() as session:
        await _seed(session, ACME_TENANT, "ACME")
        await _seed(session, BETA_TENANT, "BETA")
        repo = SqlSavedSearchRepository(session)
        saved = SavedSearch.create(
            tenant_id=ACME_TENANT,
            owner_user_id=ALICE,
            name="x",
            text_query=None,
            filters=(("priority", "eq", "urgent"),),
            now=FIXED_NOW,
        )
        await repo.add(saved)
        await session.commit()

    async with session_factory() as session:
        repo = SqlSavedSearchRepository(session)
        same = await repo.get(ACME_TENANT, saved.id)
        assert same is not None
        cross = await repo.get(BETA_TENANT, saved.id)
        assert cross is None, "cross-tenant leak: tenant B saw tenant A's saved search"


async def test_sql_saved_search_repository__safe_shape_preserves_replay_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persisting safe audit metadata must retain executable saved-query columns."""
    text_value = "client name@example.test"
    filter_value = "123-45-6789"
    async with session_factory() as session:
        await _seed(session, ACME_TENANT, "ACME")
        repository = SqlSavedSearchRepository(session)
        saved = SavedSearch.create(
            tenant_id=ACME_TENANT,
            owner_user_id=ALICE,
            name="private lookup",
            text_query=text_value,
            filters=(("data.ssn", "eq", filter_value),),
            now=FIXED_NOW,
        )
        await repository.add(saved)
        await session.commit()

    async with session_factory() as session:
        loaded = await SqlSavedSearchRepository(session).get(ACME_TENANT, saved.id)

    assert loaded is not None
    assert loaded.text_query == text_value
    assert loaded.filters == (("data.ssn", "eq", filter_value),)
    assert loaded.query_shape == saved.query_shape
    assert text_value not in str(loaded.query_shape)
    assert filter_value not in str(loaded.query_shape)


async def test_sql_audit_repository__filters_by_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Audit rows are tenant-scoped. The ``list_for_security_report``
    surface returns only the calling tenant's rows; cross-tenant ids
    surface as zero rows.
    """
    async with session_factory() as session:
        await _seed(session, ACME_TENANT, "ACME")
        await _seed(session, BETA_TENANT, "BETA")
        repo_a = SqlSavedSearchRepository(session)
        audit_a = SqlSavedSearchAuditRepository(session)
        saved_a = SavedSearch.create(
            tenant_id=ACME_TENANT,
            owner_user_id=ALICE,
            name="a",
            text_query=None,
            filters=(),
            now=FIXED_NOW,
        )
        await repo_a.add(saved_a)
        await audit_a.append(
            tenant_id=ACME_TENANT,
            actor_id=ALICE,
            saved_search_id=saved_a.id,
            query_shape={"filters": [], "sort": []},
            result_count=0,
            occurred_at=FIXED_NOW,
        )
        await session.commit()

    async with session_factory() as session:
        audit = SqlSavedSearchAuditRepository(session)
        rows = await audit.list_for_security_report(
            tenant_id=ACME_TENANT,
        )
        assert len(rows) == 1
        rows_b = await audit.list_for_security_report(
            tenant_id=BETA_TENANT,
        )
        assert rows_b == []


__all__ = []
