"""HTTP acceptance tests for role define / assign / revoke (P11-US4 / FM-048).

Covers the ``/api/v1/admin/roles`` surface that
``tests/interfaces/test_admin_roles_router.py`` leaves open (it only
exercises ``GET`` list):

- ``POST /admin/roles`` defines a role; the response matches the
  ``RoleSummary`` Pydantic schema.
- ``POST /admin/roles/{id}/assignments`` assigns; ``DELETE`` revokes.
  Revocation takes effect immediately (the live
  ``permissions_for_user`` read is empty on the next call).
- Every query is tenant-scoped: a role defined in tenant A is
  invisible to tenant B (list, assign-by-id, revoke-permission).
- Failures use the ``ErrorResponse`` envelope (``code``, ``message``,
  ``correlation_id``; never a traceback or SQL).
- The OpenAPI document contains every role-admin path.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Structure is ``// given / // when / // then``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.infrastructure.persistence.admin_roles_http_dependencies import (
    SqlAdminRolesHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlRoleRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_roles import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_X = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _run(coro: Any) -> Any:
    """Run an async repository read for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("process_admin",),
) -> FastAPI:
    """Build a minimal app with the production admin-roles provider."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.admin_roles_http_dependencies = SqlAdminRolesHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Admin",
        roles=roles,
    )
    return app


async def _permissions(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    user_id: UUID,
) -> frozenset[str]:
    async with session_factory() as session:
        return await SqlRoleRepository(session).permissions_for_user(tenant_id, user_id)


def test_define_role__valid_permissions__returns_201_role_summary(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin defining a role gets 201 with the RoleSummary wire shape."""
    # // given the admin app for tenant A
    app = _build_app(sql_session_factory)
    # // when defining a role with two live permissions
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/roles",
            json={
                "name": "case_admin",
                "description": "Synthetic case admin",
                "permissions": ["case.read", "case.create"],
            },
        )
    # // then 201 with the Pydantic RoleSummary fields
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "case_admin"
    assert body["tenant_id"] == str(TENANT_A)
    assert sorted(body["permissions"]) == ["case.create", "case.read"]
    assert body["is_reserved"] is False
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]


def test_define_role__unknown_permission__returns_409_error_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unknown permission string fails with the ErrorResponse envelope."""
    # // given the admin app for tenant A
    app = _build_app(sql_session_factory)
    # // when defining a role with a permission no Action owns
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/roles",
            json={"name": "bogus_admin", "description": "", "permissions": ["not.a.permission"]},
        )
    # // then a structured 4xx envelope — never a traceback, SQL, or bare 500
    assert response.status_code in (409, 422), response.text
    body = response.json()
    assert set(body) >= {"code", "message"}
    assert "Traceback" not in response.text
    assert "SELECT " not in response.text.upper()


def test_define_role__non_admin_actor__returns_403_error_envelope(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deny-by-default: a caller without the admin grant gets 403 + envelope."""
    # // given an app acting as a non-admin (intake role)
    app = _build_app(sql_session_factory, roles=("intake",))
    # // when attempting to define a role
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/roles",
            json={"name": "sneaky_admin", "description": "", "permissions": ["case.read"]},
        )
    # // then 403 with the forbidden envelope and no role persisted
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    app_admin = _build_app(sql_session_factory)
    with TestClient(app_admin) as client:
        listed = client.get("/api/v1/admin/roles")
    assert listed.json() == []


def test_assign_and_revoke_role__round_trip__takes_effect_immediately(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Assign grants on the next read; revoke denies on the next read."""
    # // given a defined role in tenant A
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        role_id = client.post(
            "/api/v1/admin/roles",
            json={"name": "case_admin", "description": "", "permissions": ["case.create"]},
        ).json()["id"]
        # // when assigning the role to USER_X
        assigned = client.post(
            f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(USER_X)}
        )
    assert assigned.status_code == 201, assigned.text
    assert _run(_permissions(sql_session_factory, TENANT_A, USER_X)) == frozenset({"case.create"})
    # // when revoking the assignment
    with TestClient(app) as client:
        revoked = client.delete(f"/api/v1/admin/roles/{role_id}/assignments/{USER_X}")
    # // then 204 and the very next live read grants nothing
    assert revoked.status_code == 204, revoked.text
    assert _run(_permissions(sql_session_factory, TENANT_A, USER_X)) == frozenset()


def test_revoke_permission__single_action__denies_only_that_action(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Revoking one permission narrows the role; sibling grants survive."""
    # // given a role with two permissions assigned to USER_X
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        role_id = client.post(
            "/api/v1/admin/roles",
            json={
                "name": "wide_admin",
                "description": "",
                "permissions": ["case.create", "case.read"],
            },
        ).json()["id"]
        client.post(f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(USER_X)})
        # // when revoking just case.create
        revoked = client.delete(f"/api/v1/admin/roles/{role_id}/permissions/case.create")
    # // then 200 with the narrowed set; the live read keeps only case.read
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["permissions"] == ["case.read"]
    assert _run(_permissions(sql_session_factory, TENANT_A, USER_X)) == frozenset({"case.read"})


def test_role_queries__tenant_b_actor__cannot_see_tenant_a_role(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two-tenant isolation per query: list hides; assign/revoke 404."""
    # // given a role defined + assigned in tenant A
    app_a = _build_app(sql_session_factory)
    with TestClient(app_a) as client:
        role_id = client.post(
            "/api/v1/admin/roles",
            json={"name": "case_admin", "description": "", "permissions": ["case.create"]},
        ).json()["id"]
    # // when tenant B lists, assigns, and revokes against that role id
    app_b = _build_app(sql_session_factory, user_id=ADMIN_B, tenant_id=TENANT_B)
    with TestClient(app_b) as client:
        listed = client.get("/api/v1/admin/roles")
        assigned = client.post(
            f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(uuid4())}
        )
        revoked = client.delete(f"/api/v1/admin/roles/{role_id}/assignments/{USER_X}")
    # // then B's list is empty and cross-tenant writes 404 without leaking
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    assert assigned.status_code == 404, assigned.text
    assert assigned.json()["code"] == "not_found"
    assert revoked.status_code == 404, revoked.text
    assert revoked.json()["code"] == "not_found"


def test_openapi__admin_roles_paths__define_assign_revoke_present(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The OpenAPI document exposes the define / assign / revoke contract."""
    # // given the wired admin-roles app
    app = _build_app(sql_session_factory)
    # // when reading the generated OpenAPI document
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    # // then every role-admin path of the plan §4 contract is present
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    assert "/api/v1/admin/roles" in paths
    assert "post" in paths["/api/v1/admin/roles"]  # define
    assert "get" in paths["/api/v1/admin/roles"]  # list
    assert "/api/v1/admin/roles/{role_id}/assignments" in paths  # assign
    assert "/api/v1/admin/roles/{role_id}/assignments/{user_id}" in paths  # revoke


__all__: list[str] = []
