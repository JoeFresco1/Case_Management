"""Seed explicit SQL role authority for HTTP composition tests.

Identity claims alone grant nothing when a live RoleRepository is wired.
Tests that intend an authorized actor use this helper to create the same
role and assignment rows production seeding supplies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.authorization import DEFAULT_ROLES
from app.infrastructure.persistence.models import Role, RoleAssignment, Tenant, User


async def seed_live_roles(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    role_names: tuple[str, ...],
) -> None:
    """Replace one test actor's assignments with declared built-in roles."""
    definitions = {role.name: role for role in DEFAULT_ROLES}
    unknown = set(role_names) - definitions.keys()
    if unknown:
        raise ValueError(f"unknown built-in test roles: {sorted(unknown)}")

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(
                Tenant(
                    id=tenant_id,
                    key=f"T{tenant_id.hex[:12].upper()}",
                    name="Synthetic test tenant",
                    created_at=now,
                )
            )
        if await session.get(User, user_id) is None:
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    key=f"test_{user_id.hex}",
                    display_name="Synthetic test actor",
                    roles=list(role_names),
                    is_system=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        await session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.tenant_id == tenant_id,
                RoleAssignment.user_id == user_id,
            )
        )
        for name in role_names:
            role = (
                await session.execute(
                    select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
                )
            ).scalar_one_or_none()
            if role is None:
                definition = definitions[name]
                role = Role(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    description=f"Synthetic {name} role",
                    permissions=sorted(action.value for action in definition.actions),
                    is_reserved=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(role)
                await session.flush()
            session.add(
                RoleAssignment(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role_id=role.id,
                    assigned_at=now,
                    assigned_by=None,
                )
            )
        await session.commit()
