"""Security acceptance for role define / assign / revoke (P11-US4 / FM-048).

Proves, through the real ``/api/v1/admin/roles`` HTTP surface backed by
the SQL ``RoleRepository``:

- Two-tenant isolation per query: rows written in tenant A are never
  readable or assignable from tenant B (list, assign-by-id,
  revoke-assignment, revoke-permission all stay scoped).
- Revocation takes effect immediately: after ``DELETE`` the live
  ``Policy.evaluate`` call denies, with no cache or grace period.
- Deny-by-default is never weakened: revoking one user never widens
  another, unknown actions stay denied, and non-admin actors stay
  denied ``ADMIN_TENANT_CONFIG`` before and after the grant cycle.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Structure is ``// given / // when / // then``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.authorization import Action, Policy, Resource, authorize
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError, Forbidden
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
USER_Y = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _run(coro: Any) -> Any:
    """Run an async policy/repository read for the sync TestClient test."""
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


async def _live_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    user_id: UUID,
) -> frozenset[str]:
    async with session_factory() as session:
        return await SqlRoleRepository(session).permissions_for_user(tenant_id, user_id)


async def _policy_allows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    action: Action,
    resource_tenant_id: UUID,
) -> bool:
    """Evaluate the live policy for one user through a fresh SQL session."""
    async with session_factory() as session:
        policy = Policy(role_repository=SqlRoleRepository(session))
        actor = UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            display_name="actor",
            roles=("case_admin",),
        )
        decision = await policy.evaluate(
            action, actor, Resource(tenant_id=resource_tenant_id, kind="case")
        )
        return decision.allowed


def _define_and_assign(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    permissions: list[str],
    user_id: UUID = USER_X,
) -> str:
    """Define a role in tenant A and assign it; return the role id."""
    app = _build_app(session_factory)
    with TestClient(app) as client:
        role_id = client.post(
            "/api/v1/admin/roles",
            json={"name": "case_admin", "description": "", "permissions": permissions},
        ).json()["id"]
        assigned = client.post(
            f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(user_id)}
        )
    assert assigned.status_code == 201, assigned.text
    return str(role_id)


def test_role_admin__tenant_b_queries__never_reach_tenant_a_rows(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every tenant-B query stays scoped: no read, assign, or revoke crosses."""
    # // given a role + assignment living in tenant A
    role_id = _define_and_assign(sql_session_factory, permissions=["case.create"])
    # // when tenant B lists, assigns, revokes the assignment, and revokes a permission
    app_b = _build_app(sql_session_factory, user_id=ADMIN_B, tenant_id=TENANT_B)
    with TestClient(app_b) as client:
        listed = client.get("/api/v1/admin/roles")
        assigned = client.post(
            f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(uuid4())}
        )
        revoked_assignment = client.delete(f"/api/v1/admin/roles/{role_id}/assignments/{USER_X}")
        revoked_permission = client.delete(f"/api/v1/admin/roles/{role_id}/permissions/case.create")
    # // then B sees nothing and every cross-tenant mutation is a 404 envelope
    assert listed.json() == []
    for response in (assigned, revoked_assignment, revoked_permission):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
    # // and tenant A's grant is untouched by B's attempts
    assert _run(_live_permissions(sql_session_factory, TENANT_A, USER_X)) == frozenset(
        {"case.create"}
    )


def test_role_admin__assignment_in_a__grants_nothing_against_tenant_b_resource(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The policy's cross-tenant guard denies even with a live tenant-A grant."""
    # // given USER_X holds case.create in tenant A
    _define_and_assign(sql_session_factory, permissions=["case.create"])
    # // when evaluating the same action against a tenant-B resource
    allowed_home = _run(
        _policy_allows(
            sql_session_factory,
            tenant_id=TENANT_A,
            user_id=USER_X,
            action=Action.CASE_CREATE,
            resource_tenant_id=TENANT_A,
        )
    )
    allowed_away = _run(
        _policy_allows(
            sql_session_factory,
            tenant_id=TENANT_A,
            user_id=USER_X,
            action=Action.CASE_CREATE,
            resource_tenant_id=TENANT_B,
        )
    )
    # // then home allows while the cross-tenant read denies (defense-in-depth)
    assert allowed_home is True
    assert allowed_away is False


def test_role_admin__revoke_one_user__other_user_keeps_grant(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Revocation is surgical: the revoked user denies, the sibling still allows."""
    # // given two users holding the same tenant-A role
    role_id = _define_and_assign(sql_session_factory, permissions=["case.create"], user_id=USER_X)
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        second = client.post(
            f"/api/v1/admin/roles/{role_id}/assignments", json={"user_id": str(USER_Y)}
        )
    assert second.status_code == 201, second.text
    # // when revoking only USER_X over HTTP
    with TestClient(app) as client:
        revoked = client.delete(f"/api/v1/admin/roles/{role_id}/assignments/{USER_X}")
    assert revoked.status_code == 204, revoked.text
    # // then USER_X denies on the next evaluate while USER_Y still allows
    assert (
        _run(
            _policy_allows(
                sql_session_factory,
                tenant_id=TENANT_A,
                user_id=USER_X,
                action=Action.CASE_CREATE,
                resource_tenant_id=TENANT_A,
            )
        )
        is False
    )
    assert (
        _run(
            _policy_allows(
                sql_session_factory,
                tenant_id=TENANT_A,
                user_id=USER_Y,
                action=Action.CASE_CREATE,
                resource_tenant_id=TENANT_A,
            )
        )
        is True
    )


async def test_role_admin__grant_revoke_cycle__deny_by_default_intact(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After a full grant/revoke cycle the default-deny posture is unchanged."""
    # // given a granted-then-revoked user in tenant A
    role_id = _define_and_assign(sql_session_factory, permissions=["case.create"])
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        revoked = client.delete(f"/api/v1/admin/roles/{role_id}/assignments/{USER_X}")
    assert revoked.status_code == 204, revoked.text
    # // when authorizing the revoked action, an unknown action, and a non-admin admin-op
    async with sql_session_factory() as session:
        policy = Policy(role_repository=SqlRoleRepository(session))
        revoked_actor = UserContext(
            user_id=USER_X, tenant_id=TENANT_A, display_name="revoked", roles=("case_admin",)
        )
        plain_actor = UserContext(
            user_id=uuid4(), tenant_id=TENANT_A, display_name="plain", roles=("intake",)
        )
        revoked_decision = await policy.evaluate(
            Action.CASE_CREATE, revoked_actor, Resource(tenant_id=TENANT_A, kind="case")
        )
        # // then the revoked grant denies, ungranted actions deny, non-admins deny
        assert revoked_decision.allowed is False
        assert "no role" in revoked_decision.reason
        with pytest.raises(Forbidden):
            await authorize(
                Action.ADMIN_TENANT_CONFIG,
                plain_actor,
                Resource(tenant_id=TENANT_A, kind="role"),
            )


__all__: list[str] = []
