"""Publication history inspect + concurrent-conflict + approval-chain tests (FM-021).

Covers the task acceptance:

- concurrent config publish returns a structured 409 conflict with no
  silent overwrite (second publish of the same version is refused,
  first publisher stays authoritative);
- inspect shows the full schema with author (published_by) and
  effective time (published_at);
- governance history shows author, approval, effective time, and
  change summary across the publish -> approve chain;
- migration 0028 downgrades cleanly on sqlite.
"""

from __future__ import annotations

import asyncio
import os as _os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.deps import UserContext, get_clock, get_current_user, get_db
from app.domain.case_type import CaseType
from app.domain.errors import DomainError
from app.infrastructure.persistence.admin_case_types_http_dependencies import (
    SqlAdminCaseTypesHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_governance_http_dependencies import (
    SqlAdminGovernanceHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlCaseTypeRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_case_types import router as case_types_router
from app.interfaces.http.routers.admin_governance import router as governance_router
from tests._live_roles import seed_live_roles

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"

_SCHEMA = {
    "fields": [{"key": "title", "label": "Title", "type": "text", "required": True}],
    "document_classifications": ["Contract"],
    "rules": [],
}


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_case_type(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        await SqlCaseTypeRepository(session).add(
            CaseType.create(tenant_id=TENANT_ID, key="ServiceRequest", name="Service Request")
        )
        await session.commit()


def _build_app(session_factory: async_sessionmaker[AsyncSession], clock: Any) -> FastAPI:
    _run(
        seed_live_roles(
            session_factory,
            tenant_id=TENANT_ID,
            user_id=ADMIN_A,
            role_names=("process_admin",),
        )
    )

    async def _override_get_db() -> Any:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.state.admin_case_types_http_dependencies = SqlAdminCaseTypesHttpDependencyProvider()
    app.state.admin_governance_http_dependencies = SqlAdminGovernanceHttpDependencyProvider()
    app.include_router(case_types_router, prefix="/api/v1")
    app.include_router(governance_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_A,
        tenant_id=TENANT_ID,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    app.dependency_overrides[__import__("app.config", fromlist=["get_settings"]).get_settings] = (
        lambda: Settings(app_env="local")
    )
    return app


def _draft_and_publish(client: TestClient) -> dict[str, Any]:
    # // given a draft version exists
    draft = client.post(
        "/api/v1/admin/case-types/ServiceRequest/versions", json={"schema": _SCHEMA}
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["case_type_version_id"]
    # // when published once
    first = client.post(f"/api/v1/admin/case-types/ServiceRequest/versions/{version_id}/publish")
    assert first.status_code == 200, first.text
    return {"version_id": version_id, "publish": first.json()}


def test_admin_case_types__concurrent_publish__structured_conflict_no_overwrite(
    sql_session_factory: async_sessionmaker[AsyncSession], clock: Any
) -> None:
    """A repeated publish of the same version returns 409 conflict."""
    # // given a published version
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)
    with TestClient(app) as client:
        ctx = _draft_and_publish(client)
        version_id = ctx["version_id"]
        # // when a concurrent publisher retries the same publish
        retry = client.post(
            f"/api/v1/admin/case-types/ServiceRequest/versions/{version_id}/publish"
        )
        # // then a structured conflict is returned, not 500 or silent 200
        assert retry.status_code == 409, retry.text
        body = retry.json()
        assert body["code"] == "conflict"
        assert "correlation_id" in body
        # // and the version still shows the first publisher as author
        history = client.get("/api/v1/admin/case-types/ServiceRequest/versions")
        assert history.status_code == 200, history.text
        entry = history.json()["versions"][0]
        assert entry["status"] == "published"
        assert entry["published_by"] == str(ADMIN_A)


def test_admin_case_types__inspect__shows_full_schema_author_effective_time(
    sql_session_factory: async_sessionmaker[AsyncSession], clock: Any
) -> None:
    """Inspect returns the full schema with author and effective time."""
    # // given a published version
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)
    with TestClient(app) as client:
        ctx = _draft_and_publish(client)
        version_id = ctx["version_id"]
        # // when inspected by id
        response = client.get(f"/api/v1/admin/case-types/ServiceRequest/versions/{version_id}")
        # // then the full schema plus lineage is returned
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == version_id
        assert body["status"] == "published"
        assert body["published_by"] == str(ADMIN_A)
        assert body["published_at"] is not None
        assert body["schema"]["fields"][0]["key"] == "title"
        assert body["schema"]["document_classifications"] == ["Contract"]
        assert body["field_keys"] == ["title"]


def test_admin_case_types__inspect__cross_key_returns_not_found(
    sql_session_factory: async_sessionmaker[AsyncSession], clock: Any
) -> None:
    """Inspect is key-scoped: a valid id under the wrong key is 404."""
    # // given a published version under ServiceRequest
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)
    with TestClient(app) as client:
        ctx = _draft_and_publish(client)
        # // when inspected under an unknown key
        response = client.get(f"/api/v1/admin/case-types/NoSuchType/versions/{ctx['version_id']}")
        # // then not found, never a cross-key row
        assert response.status_code == 404, response.text


def test_governance_history__publish_then_approve__shows_author_approval_effective_summary(
    sql_session_factory: async_sessionmaker[AsyncSession], clock: Any
) -> None:
    """History shows author, approval, effective time, and change summary."""
    # // given a published version with a recorded publish row
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)
    with TestClient(app) as client:
        ctx = _draft_and_publish(client)
        version_id = ctx["version_id"]
        history = client.get("/api/v1/admin/case-types/ServiceRequest/versions")
        case_type_id = history.json()["case_type_id"]

        publish = client.post(
            "/api/v1/admin/publications",
            json={
                "artifact_type": "CaseTypeVersion",
                "artifact_id": case_type_id,
                "version_id": version_id,
                "action": "publish",
                "change_summary": "Initial publish of ServiceRequest v1",
            },
        )
        assert publish.status_code == 201, publish.text

        # // when a second admin approves
        approval = client.post(
            "/api/v1/admin/publications/approve",
            json={
                "artifact_type": "CaseTypeVersion",
                "artifact_id": case_type_id,
                "version_id": version_id,
                "approving_actor_id": str(ADMIN_B),
                "change_summary": "Second-admin review passed",
            },
        )
        assert approval.status_code == 201, approval.text

        # // then history shows author, approval, effective time, summary
        timeline = client.get(
            "/api/v1/admin/publications",
            params={"artifact_type": "CaseTypeVersion", "artifact_id": case_type_id},
        )
        assert timeline.status_code == 200, timeline.text
        items = timeline.json()["items"]
        by_action = {item["action"]: item for item in items}
        assert by_action["publish"]["actor_id"] == str(ADMIN_A)
        assert by_action["publish"]["change_summary"] == "Initial publish of ServiceRequest v1"
        assert by_action["publish"]["occurred_at"] is not None
        assert by_action["validate"]["actor_id"] == str(ADMIN_B)
        assert by_action["validate"]["change_summary"] == "Second-admin review passed"
        assert by_action["validate"]["occurred_at"] >= by_action["publish"]["occurred_at"]


def test_governance__same_actor_approve__forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession], clock: Any
) -> None:
    """Separation-of-duties: the publisher cannot approve their own publish."""
    # // given a recorded publish by ADMIN_A
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)
    with TestClient(app) as client:
        ctx = _draft_and_publish(client)
        history = client.get("/api/v1/admin/case-types/ServiceRequest/versions")
        case_type_id = history.json()["case_type_id"]
        client.post(
            "/api/v1/admin/publications",
            json={
                "artifact_type": "CaseTypeVersion",
                "artifact_id": case_type_id,
                "version_id": ctx["version_id"],
                "action": "publish",
                "change_summary": "self-approval attempt",
            },
        )
        # // when the same admin approves
        response = client.post(
            "/api/v1/admin/publications/approve",
            json={
                "artifact_type": "CaseTypeVersion",
                "artifact_id": case_type_id,
                "version_id": ctx["version_id"],
                "approving_actor_id": str(ADMIN_A),
                "change_summary": "self approval",
            },
        )
        # // then forbidden
        assert response.status_code == 403, response.text


def test_migration_0028__upgrade_downgrade__roundtrip_cleanly_on_sqlite(tmp_path: Path) -> None:
    """0028 creates both indexes on upgrade and drops them on downgrade."""
    # // given a scratch sqlite file and no other state
    db_file = tmp_path / "pub_inspect_0028.db"
    backend = str(BACKEND_DIR)
    sqlite_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: PLW1510
            [sys.executable, "-m", "alembic", *args],
            cwd=backend,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={**_os.environ, "APP_ENV": "test", "DATABASE_URL": sqlite_url},
        )

    # Why pinned to the 0028 revision (not head): the test asserts
    # that ``downgrade -1`` removes exactly the 0028 indexes. Later
    # revisions (e.g. 0029 viewer tables) move head forward, so an
    # unpinned ``upgrade head`` would make ``downgrade -1`` land on
    # 0028 with the indexes still present. Pinning keeps the test
    # about 0028 regardless of newer heads.
    upgrade = _alembic("upgrade", "0028_publication_inspect_indexes")
    assert upgrade.returncode == 0, f"upgrade 0028 failed:\n{upgrade.stdout}\n{upgrade.stderr}"

    def _indexes() -> dict[str, list[str]]:
        engine = create_engine(f"sqlite:///{db_file.as_posix()}")
        try:
            insp = sa_inspect(engine)
            out: dict[str, list[str]] = {}
            for table in ("configuration_publications", "case_type_versions"):
                out[table] = sorted(i["name"] for i in insp.get_indexes(table))
            return out
        finally:
            engine.dispose()

    # // when at head
    head_indexes = _indexes()
    pub_indexes = head_indexes["configuration_publications"]
    ctv_indexes = head_indexes["case_type_versions"]
    assert "ix_configuration_publications_tenant_version" in pub_indexes
    assert "ix_case_type_versions_tenant_case_version" in ctv_indexes

    # // when downgraded one revision (0028 only)
    downgrade = _alembic("downgrade", "-1")
    assert downgrade.returncode == 0, (
        f"downgrade -1 failed:\n{downgrade.stdout}\n{downgrade.stderr}"
    )
    rolled_back = _indexes()
    assert (
        "ix_configuration_publications_tenant_version"
        not in (rolled_back["configuration_publications"])
    )
    assert "ix_case_type_versions_tenant_case_version" not in rolled_back["case_type_versions"]

    # // then replay to head restores them
    replay = _alembic("upgrade", "head")
    assert replay.returncode == 0, f"replay failed:\n{replay.stdout}\n{replay.stderr}"
    restored = _indexes()
    assert "ix_configuration_publications_tenant_version" in restored["configuration_publications"]

    _ = uuid4  # keep import used if future cases need synthetic ids
