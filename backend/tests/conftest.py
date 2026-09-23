"""Top-level pytest fixtures and configuration for ``backend/tests``.

Why this file exists
--------------------
AGENTS.md §4.R5 prescribes an organized, sortable test suite with
``test_<unit>_<scenario>__<expectation>`` naming, ``// given / // when /
// then`` structure, and in-memory fakes preferred over a live database.
Each per-module file already declares its own narrow fixtures (see e.g.
``tests/application/cases/test_create_case.py``) so this conftest
intentionally stays minimal:

* ``settings_test`` — a ``Settings`` instance pinned to ``APP_ENV=test``
  with sqlite, no auth header leakage, and a fixed clock.
* ``clock`` — a fixed deterministic ``Clock`` (UTC 2026-08-31 12:00).
  Per-module fixtures that need a different shape (e.g.
  ``tests/application/test_legal_hold.py``'s ``_MutableClock``) win
  because pytest fixture resolution prefers the closest ``conftest.py``.
* ``tenant_a`` / ``tenant_b`` — two well-known UUIDs used by every
  tenant-isolation parametrized test.
* ``intake_user`` / ``reviewer_user`` / ``supervisor_user`` — three
  actors with distinct roles for auth-denial and SoD scenarios.
* ``intake_user_context`` / ``no_user_context`` — pre-built
  ``UserContext`` values for use-case handlers.
* ``sqlite_db_url`` — a per-test sqlite URL under ``tmp_path`` so
  domain/application tests that need the real SQL repositories never
  share state.
* ``sql_session_factory`` — an ``async_sessionmaker`` bound to a fresh
  sqlite schema, with the tenant listener disabled so acceptance
  scenarios prove the explicit ``WHERE tenant_id = :tenant_id``
  filter is sufficient on its own.
* ``in_memory_*`` re-exports of the canonical fakes so tests can import
  them from ``tests.conftest`` without coupling to a specific use-case
  module.
* ``_suppress_testclient_deprecation`` — installed by
  ``pytest_configure`` to ignore the pre-existing Starlette
  ``HTTP_422_UNPROCESSABLE_ENTITY`` and ``on_event`` deprecations that
  fire when ``app.main`` is imported anywhere in the test graph. This
  is the same suppression the integration sub-suite uses
  (``tests/integration/conftest.py``); it is duplicated here so the
  whole tree benefits, not just the integration directory.

Sortability
-----------
All test ids collected under this layout are already alphabetical
because the directory structure mirrors the source layout
(``backend/app`` → ``backend/tests``) and pytest sorts by file path
before line number. ``test_conventions.py`` enforces that contract.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.db import Base, install_tenant_listener, reset_engine
from app.deps import CorrelationContext, UserContext, clear_local_users

# Re-exported fakes so test modules can write ``from tests.conftest import
# InMemoryCaseRepository`` without coupling to a specific use-case
# submodule. The originals still live next to the test that exercises
# them; the canonical home is ``tests/application/cases/_fakes.py``.
from tests.application.cases._fakes import (  # noqa: E402,F401
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRelationshipRepository,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryDocumentRepository,
    InMemoryIdempotencyStore,
    InMemoryOutbox,
    make_case_type,
)

FIXED_NOW: datetime = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B: UUID = UUID("22222222-2222-2222-2222-222222222222")

INTAKE_USER: UUID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_USER: UUID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SUPERVISOR_USER: UUID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def pytest_configure(config: pytest.Config) -> None:
    """Re-register ignores for deprecations ``filterwarnings = ["error"]`` re-promotes.

    The integration sub-suite also installs these filters; doing it here
    ensures every test (not just ``tests/integration``) can import
    ``app.main`` without the deprecation raising.

    M-3 / ADR-TRACK: These suppressions are a pragmatic compromise.
    ``filterwarnings = ["error"]`` (AGENTS.md §4.R5) re-promotes
    DeprecationWarnings to errors at collection time. Starlette/FastAPI/httpx
    periodically add new deprecations that surface when ``app.main`` is
    imported. We explicitly ignore the known ones here. When upstream
    libraries release new deprecations, collection will fail and the
    suppression list must be updated. Track replacing httpx with httpx2 and
    migrating FastAPI from ``on_event`` to lifespan API in the foundation
    backlog.
    """
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*HTTP_422_UNPROCESSABLE_ENTITY.*",
    )
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*on_event is deprecated.*",
    )
    # ``filterwarnings = ["error"]`` in pyproject.toml re-promotes any
    # warning. The Starlette test client emits an ``httpx2`` notice that
    # the wider ecosystem has not yet absorbed; we ignore it here so
    # collection doesn't break. Track replacing httpx with httpx2 in
    # the foundation backlog.
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*httpx.*starlette.testclient.*",
    )


@pytest.fixture(scope="session")
def settings_test() -> Settings:
    """A ``Settings`` pinned to the sqlite test profile."""
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./.pytest.db")
    os.environ.setdefault("SYNTHETIC_ENVIRONMENT", "1")
    return Settings()


@pytest.fixture
def clock() -> FixedClock:
    """Deterministic UTC clock used by every domain/application test.

    Tests that need a mutable clock (e.g. SLA escalation) declare their
    own fixture in the same module; pytest resolves the closest
    ``conftest.py`` first, so the local fixture wins.
    """
    return FixedClock(FIXED_NOW)


@pytest.fixture
def fixed_now() -> datetime:
    """The same fixed instant as ``clock()`` returned by ``clock.now()``."""
    return FIXED_NOW


@pytest.fixture
def tenant_a() -> UUID:
    return TENANT_A


@pytest.fixture
def tenant_b() -> UUID:
    return TENANT_B


@pytest.fixture
def intake_user() -> UUID:
    return INTAKE_USER


@pytest.fixture
def reviewer_user() -> UUID:
    return REVIEWER_USER


@pytest.fixture
def supervisor_user() -> UUID:
    return SUPERVISOR_USER


@pytest.fixture
def intake_user_context(tenant_a: UUID, intake_user: UUID) -> UserContext:
    return UserContext(
        user_id=intake_user,
        tenant_id=tenant_a,
        roles=("intake",),
        is_system=False,
    )


@pytest.fixture
def reviewer_user_context(tenant_a: UUID, reviewer_user: UUID) -> UserContext:
    return UserContext(
        user_id=reviewer_user,
        tenant_id=tenant_a,
        roles=("reviewer",),
        is_system=False,
    )


@pytest.fixture
def no_user_context() -> UserContext | None:
    """Sentinel ``None`` for the auth-denial scenario.

    Use case handlers must raise ``Unauthorized`` before any side
    effect when no user is supplied.
    """
    return None


@pytest.fixture
def correlation_id() -> CorrelationContext:
    return CorrelationContext(correlation_id="test-correlation")


@pytest.fixture
def sqlite_db_url(tmp_path: Path) -> str:
    """A unique sqlite URL per test, no shared state between tests."""
    db_file = tmp_path / "conftest.db"
    return f"sqlite+aiosqlite:///{db_file.as_posix()}"


@pytest.fixture
async def sql_engine(
    sqlite_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    """A fresh sqlite engine with the tenant listener DISABLED.

    Per AGENTS.md §4.R10, the listener is defense-in-depth; the primary
    contract is that explicit ``WHERE tenant_id = :tenant_id`` filters
    in every repository are sufficient on their own.
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
def sql_session_factory(
    sql_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=sql_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def sql_session(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sql_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# In-memory fakes — convenience aliases that re-bind the canonical fakes
# from ``tests.application.cases._fakes`` to fixtures. Tests can ask for
# either ``in_memory_case_repository`` (fixture) or instantiate the class
# directly; the fixture form is preferred for use cases that only need
# one repository.
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_case_repository() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def in_memory_case_type_repository() -> InMemoryCaseTypeRepository:
    return InMemoryCaseTypeRepository()


@pytest.fixture
def in_memory_case_type_version_repository() -> InMemoryCaseTypeVersionRepository:
    return InMemoryCaseTypeVersionRepository()


@pytest.fixture
def in_memory_audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def in_memory_outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


@pytest.fixture
def in_memory_idempotency_store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def in_memory_document_repository() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def in_memory_case_relationship_repository() -> InMemoryCaseRelationshipRepository:
    return InMemoryCaseRelationshipRepository()


# ---------------------------------------------------------------------------
# Tenant-isolation parametrization. AGENTS.md §4.R10 mandates a
# two-tenant parametrized test for every repository; the helper below is
# used by every test that wants to iterate "for each tenant in {A, B}"
# while asserting the other tenant cannot see the row.
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_ids() -> tuple[UUID, UUID]:
    return (TENANT_A, TENANT_B)


# Pytest helper: builds a Settings instance for use in tests that need
# to override a single field without instantiating a full Settings.
@pytest.fixture
def make_settings() -> Any:
    def _make(**overrides: Any) -> Settings:
        return Settings(**overrides)

    return _make


@pytest.fixture(autouse=True)
def _clear_local_users():
    """Clear the local user registry before each test to ensure isolation."""
    clear_local_users()
    yield
    clear_local_users()
