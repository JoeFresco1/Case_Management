"""Seed CLI integration tests for persisted local identities and roles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.authorization import DEFAULT_ROLES
from app.cli.seed import runner
from app.cli.seed.dataset import TENANT_KEY, TENANT_NAME, _synthetic_users
from app.cli.seed.runner import _seed_builtin_roles, _seed_user_roles
from app.config import Settings
from app.infrastructure.persistence import models


@pytest.mark.asyncio
async def test_seed__legacy_users_without_roles__bootstraps_once_and_preserves_revocation(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first role-aware seed upgrades old users, later runs keep revocations."""
    tenant_id = uuid4()
    users = _synthetic_users(tenant_id)
    now = datetime.now(tz=UTC)
    async with sql_session_factory() as session:
        session.add(
            models.Tenant(
                id=tenant_id,
                key=TENANT_KEY,
                name=TENANT_NAME,
                created_at=now,
            )
        )
        session.add_all(
            [
                models.User(
                    id=user.user_id,
                    tenant_id=tenant_id,
                    key=user.key,
                    display_name=user.display_name,
                    roles=list(user.roles),
                    is_system=False,
                    created_at=now,
                    updated_at=now,
                )
                for user in users
            ]
        )
        await session.commit()

    async with sql_session_factory() as session:
        role_rows, should_bootstrap = await _seed_builtin_roles(session, tenant_id)
        assert should_bootstrap
        for user in users:
            user_row = await session.get(models.User, user.user_id)
            assert user_row is not None
            await _seed_user_roles(
                session,
                tenant_id=tenant_id,
                user_row=user_row,
                role_rows=role_rows,
                role_names=user.roles,
                grant_missing=should_bootstrap,
            )
        await session.commit()

    async with sql_session_factory() as session:
        role_rows = (
            (await session.execute(select(models.Role).where(models.Role.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        assignment_rows = (
            (
                await session.execute(
                    select(models.RoleAssignment).where(
                        models.RoleAssignment.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.name for row in role_rows} == {role.name for role in DEFAULT_ROLES}
        assert len(assignment_rows) == len(users)

        admin_user_id = next(user.user_id for user in users if user.key == "process_admin")
        admin_role_id = next(row.id for row in role_rows if row.name == "process_admin")
        await session.execute(
            delete(models.RoleAssignment).where(
                models.RoleAssignment.tenant_id == tenant_id,
                models.RoleAssignment.user_id == admin_user_id,
                models.RoleAssignment.role_id == admin_role_id,
            )
        )
        await session.commit()

    # Reseeding must preserve a deliberate admin-role revocation.
    async with sql_session_factory() as session:
        role_rows, should_bootstrap = await _seed_builtin_roles(session, tenant_id)
        assert not should_bootstrap
        for user in users:
            user_row = await session.get(models.User, user.user_id)
            assert user_row is not None
            await _seed_user_roles(
                session,
                tenant_id=tenant_id,
                user_row=user_row,
                role_rows=role_rows,
                role_names=user.roles,
                grant_missing=should_bootstrap,
            )
        await session.commit()

    async with sql_session_factory() as session:
        remaining = (
            (
                await session.execute(
                    select(models.RoleAssignment).where(
                        models.RoleAssignment.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == len(users) - 1
        assert not any(
            row.user_id == admin_user_id and row.role_id == admin_role_id for row in remaining
        )


@pytest.mark.asyncio
async def test_seed__fresh_sqlite__creates_demo_and_process_admin(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented seed path completes on a fresh SQLite schema."""

    @asynccontextmanager
    async def _test_session_scope() -> AsyncIterator[AsyncSession]:
        async with sql_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(runner, "get_settings", lambda: Settings(app_env="test"))
    monkeypatch.setattr(runner, "session_scope", _test_session_scope)

    summary = await runner._seed()
    tenant_id = UUID(summary["tenant"]["id"])

    async with sql_session_factory() as session:
        user_rows = (
            (await session.execute(select(models.User).where(models.User.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        case_rows = (
            (await session.execute(select(models.Case).where(models.Case.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        assignment_rows = (
            (
                await session.execute(
                    select(models.RoleAssignment).where(
                        models.RoleAssignment.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.key for row in user_rows} == {
        "intake",
        "reviewer",
        "supervisor",
        "process_admin",
    }
    assert len(case_rows) == 1
    assert len(assignment_rows) == len(user_rows)
