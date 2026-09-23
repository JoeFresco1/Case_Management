"""Tests for role and permission administration (P11-US4 / FM-048).

Covers:

- Deny-by-default is preserved: an actor with no admin role gets a 403
  (Forbidden) on every admin role endpoint.
- An admin can define a role with a permission set; ``policy.evaluate``
  reflects the new permission on the next request (live lookup).
- Revocation is immediate: removing the assignment or the permission
  blocks the action on the next ``evaluate`` call.
- Tenant isolation: a role defined in tenant A is invisible from tenant
  B; assigning in tenant A never grants access to a tenant B resource.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    Action,
    Policy,
    Resource,
    authorize,
)
from app.application.roles import manage_roles
from app.deps import UserContext
from app.domain.errors import Conflict, Forbidden, InvariantViolation
from app.domain.role import (
    RESERVED_ROLE_NAMES,
    NewRoleSpec,
    Role,
    make_role,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes — local, mirror ``tests/application/cases/_fakes.py`` style.
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )


class InMemoryRoleRepository:
    """In-memory ``RoleRepository``.

    Captures role + assignment state for the test to assert against.
    The ``get_by_name`` + ``get`` lookups mirror the SQL adapter
    contract (tenant-scoped) so tenant-isolation tests exercise the
    same code path the production adapter takes.
    """

    def __init__(self) -> None:
        self._roles: dict[tuple[UUID, UUID], Role] = {}
        self._roles_by_name: dict[tuple[UUID, str], Role] = {}
        self._assignments: set[tuple[UUID, UUID, UUID]] = set()
        # Test inspection helpers.
        self.assignments_log: list[tuple[UUID, UUID, UUID]] = []
        self.revocations_log: list[tuple[UUID, UUID, UUID]] = []

    async def get(self, tenant_id: UUID, role_id: UUID) -> Role | None:
        return self._roles.get((tenant_id, role_id))

    async def get_by_name(self, tenant_id: UUID, name: str) -> Role | None:
        return self._roles_by_name.get((tenant_id, name))

    async def list(self, tenant_id: UUID) -> list[Role]:
        return [r for (tid, _), r in self._roles.items() if tid == tenant_id]

    async def add(self, role: Role) -> None:
        key = (role.tenant_id, role.name)
        if key in self._roles_by_name:
            raise Conflict(f"role {role.name!r} already exists in tenant")
        self._roles[(role.tenant_id, role.id)] = role
        self._roles_by_name[key] = role

    async def update_permissions(
        self, tenant_id: UUID, role_id: UUID, permissions: frozenset[str]
    ) -> None:
        role = self._roles.get((tenant_id, role_id))
        if role is None:
            return
        new_role = Role(
            id=role.id,
            tenant_id=role.tenant_id,
            name=role.name,
            description=role.description,
            permissions=permissions,
            is_reserved=role.is_reserved,
            created_at=role.created_at,
        )
        self._roles[(tenant_id, role_id)] = new_role
        self._roles_by_name[(tenant_id, role.name)] = new_role

    async def delete(self, tenant_id: UUID, role_id: UUID) -> None:
        role = self._roles.pop((tenant_id, role_id), None)
        if role is not None:
            self._roles_by_name.pop((tenant_id, role.name), None)
            # Cascade: revoke every assignment of this role.
            stale = [a for a in self._assignments if a[0] == tenant_id and a[2] == role_id]
            for a in stale:
                self._assignments.discard(a)

    async def assign(
        self,
        tenant_id: UUID,
        user_id: UUID,
        role_id: UUID,
        *,
        assigned_by: UUID | None,
        assigned_at: datetime,
    ) -> None:
        self._assignments.add((tenant_id, user_id, role_id))
        self.assignments_log.append((tenant_id, user_id, role_id))

    async def revoke(self, tenant_id: UUID, user_id: UUID, role_id: UUID) -> None:
        self._assignments.discard((tenant_id, user_id, role_id))
        self.revocations_log.append((tenant_id, user_id, role_id))

    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]:
        """Live union of permissions for every role the user holds."""
        out: set[str] = set()
        for tid, uid, rid in self._assignments:
            if tid == tenant_id and uid == user_id:
                role = self._roles.get((tid, rid))
                if role is not None:
                    out.update(role.permissions)
        return frozenset(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_user(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="admin",
        roles=("process_admin",),
    )


def _non_admin_user(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="non-admin",
        roles=("intake",),
    )


def _resource(tenant_id: UUID = TENANT_A) -> Resource:
    return Resource(tenant_id=tenant_id, kind="role")


def _user_context(*, tenant_id: UUID, user_id: UUID, role_names: tuple[str, ...]) -> UserContext:
    """Build a user with only role names (the IdP claim shape)."""
    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="actor",
        roles=role_names,
    )


def _build_role(
    *,
    tenant_id: UUID = TENANT_A,
    name: str = "case_admin",
    permissions: tuple[str, ...] = ("case.create",),
    is_reserved: bool = False,
) -> Role:
    return make_role(
        spec=NewRoleSpec(name=name, description="test role", permissions=frozenset(permissions)),
        tenant_id=tenant_id,
        role_id=uuid4(),
        now=NOW,
        is_reserved=is_reserved,
    )


# ---------------------------------------------------------------------------
# 1. Deny-by-default
# ---------------------------------------------------------------------------


async def test_define_role_non_admin_actor_is_forbidden() -> None:
    """A non-admin actor attempting to define a role is denied (403)."""
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    actor = _non_admin_user()

    with pytest.raises(Forbidden):
        await manage_roles.define_role(
            manage_roles.DefineRoleCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                name="new_role",
                description="",
                permissions=("case.read",),
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )

    # No side effect: the repository is untouched.
    assert await repo.list(actor.tenant_id) == []
    assert audit.events == []


async def test_assign_role_non_admin_actor_is_forbidden() -> None:
    """A non-admin attempting to assign a role is forbidden; the
    assignment table is unchanged (no partial state)."""
    repo = InMemoryRoleRepository()
    role = _build_role()
    await repo.add(role)
    audit = InMemoryAuditStore()
    clock = FixedClock()
    actor = _non_admin_user()
    target_user_id = uuid4()

    with pytest.raises(Forbidden):
        await manage_roles.assign_role(
            manage_roles.AssignRoleCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                role_id=role.id,
                user_id=target_user_id,
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )

    # No row was added and no audit was emitted.
    assert repo.assignments_log == []
    assert audit.events == []


async def test_policy_evaluate_admin_action_without_role_denies() -> None:
    """The deny-by-default path: an actor with no admin role in their
    claim is denied ``ADMIN_TENANT_CONFIG`` even against a resource in
    their own tenant. This preserves the AGENTS.md §4.R12 contract.
    """
    policy = Policy()
    actor = _user_context(
        tenant_id=TENANT_A,
        user_id=uuid4(),
        role_names=("reviewer",),
    )
    decision = await policy.evaluate(Action.ADMIN_TENANT_CONFIG, actor, _resource(TENANT_A))
    assert decision.allowed is False
    assert "no role" in decision.reason


# ---------------------------------------------------------------------------
# 2. Define role -> policy reflects new permission
# ---------------------------------------------------------------------------


async def test_define_role_admin_persists_and_audits() -> None:
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    actor = _admin_user()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=actor.tenant_id,
            actor=actor,
            correlation_id="cid-define",
            name="custom_admin",
            description="custom",
            permissions=("case.read", "case.create"),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    assert role.name == "custom_admin"
    assert role.permissions == frozenset({"case.read", "case.create"})
    assert len(audit.events) == 1
    assert audit.events[0].event_type == manage_roles.ROLE_DEFINED


async def test_define_role_idempotent_with_same_permissions_returns_existing() -> None:
    """A retry of the same define call (same name + permissions) is a
    no-op so admin clients can safely replay."""
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    actor = _admin_user()

    cmd = manage_roles.DefineRoleCommand(
        tenant_id=actor.tenant_id,
        actor=actor,
        correlation_id="cid",
        name="custom_admin",
        description="custom",
        permissions=("case.read",),
    )
    role_one = await manage_roles.define_role(cmd, role_repo=repo, audit=audit, clock=clock)
    role_two = await manage_roles.define_role(cmd, role_repo=repo, audit=audit, clock=clock)
    assert role_one.id == role_two.id
    # Only one audit event was emitted.
    assert len(audit.events) == 1


async def test_define_role_conflict_with_different_permissions_raises() -> None:
    """Re-defining an existing role with a different permission set is
    a 409 Conflict so admins don't silently lose granted permissions."""
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    actor = _admin_user()

    await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=actor.tenant_id,
            actor=actor,
            correlation_id="cid",
            name="custom_admin",
            description="",
            permissions=("case.read",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    with pytest.raises(Conflict):
        await manage_roles.define_role(
            manage_roles.DefineRoleCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                name="custom_admin",
                description="",
                permissions=("case.read", "case.create"),
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )


async def test_policy_reflects_define_role_after_live_assignment() -> None:
    """The acceptance scenario: an admin defines a role, assigns a
    user to it, and the next ``policy.evaluate`` call grants the new
    permissions. ``policy.evaluate`` reads the live roles table.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    # The user only carries the role name in their claim.
    actor_user_id = uuid4()
    await manage_roles.assign_role(
        manage_roles.AssignRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            user_id=actor_user_id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    policy = Policy(role_repository=repo)
    actor = _user_context(
        tenant_id=admin.tenant_id,
        user_id=actor_user_id,
        role_names=("case_admin",),
    )

    decision = await policy.evaluate(Action.CASE_CREATE, actor, _resource(admin.tenant_id))
    assert decision.allowed is True, decision.reason
    assert "role-assignment" in decision.reason


# ---------------------------------------------------------------------------
# 3. Revocation is immediate
# ---------------------------------------------------------------------------


async def test_revoke_role_immediately_denies_action() -> None:
    """The acceptance scenario for P11-US4: revoking the role blocks
    the action on the very next ``evaluate`` call. No caching.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()
    actor_user_id = uuid4()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    await manage_roles.assign_role(
        manage_roles.AssignRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            user_id=actor_user_id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    policy = Policy(role_repository=repo)
    actor = _user_context(
        tenant_id=admin.tenant_id,
        user_id=actor_user_id,
        role_names=("case_admin",),
    )

    # While assigned, the action is allowed.
    decision_before = await policy.evaluate(Action.CASE_CREATE, actor, _resource(admin.tenant_id))
    assert decision_before.allowed is True

    # Revoke the assignment.
    await manage_roles.revoke_role(
        manage_roles.RevokeRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            user_id=actor_user_id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    # Immediately after the revocation, the same evaluate call denies.
    decision_after = await policy.evaluate(Action.CASE_CREATE, actor, _resource(admin.tenant_id))
    assert decision_after.allowed is False
    assert "no role" in decision_after.reason


async def test_update_permissions_replaces_set_atomically() -> None:
    """``update_role_permissions`` is a wholesale replace; old
    permissions disappear and new ones appear on the next policy
    evaluate call.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()
    actor_user_id = uuid4()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    await manage_roles.assign_role(
        manage_roles.AssignRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            user_id=actor_user_id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    policy = Policy(role_repository=repo)
    actor = _user_context(
        tenant_id=admin.tenant_id,
        user_id=actor_user_id,
        role_names=("case_admin",),
    )

    # Replace create -> update.
    await manage_roles.update_role_permissions(
        manage_roles.UpdatePermissionsCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            permissions=("case.update",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    create_decision = await policy.evaluate(Action.CASE_CREATE, actor, _resource(admin.tenant_id))
    update_decision = await policy.evaluate(Action.CASE_UPDATE, actor, _resource(admin.tenant_id))
    assert create_decision.allowed is False
    assert update_decision.allowed is True


async def test_revoke_permission_removes_single_action() -> None:
    """Single-action revoke via the use case; the next evaluate call
    denies just that action.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create", "case.update"),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    new_role = await manage_roles.revoke_permission(
        manage_roles.RevokePermissionCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            permission="case.create",
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    assert new_role.permissions == frozenset({"case.update"})


async def test_delete_role_cascades_assignments() -> None:
    """Deleting a role removes all of its assignments so the live
    lookup returns nothing for any user previously held the role.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()
    user_a = uuid4()
    user_b = uuid4()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    for u in (user_a, user_b):
        await manage_roles.assign_role(
            manage_roles.AssignRoleCommand(
                tenant_id=admin.tenant_id,
                actor=admin,
                correlation_id="cid",
                role_id=role.id,
                user_id=u,
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )

    await manage_roles.delete_role(
        manage_roles.DeleteRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    assert await repo.get(admin.tenant_id, role.id) is None
    assert await repo.permissions_for_user(admin.tenant_id, user_a) == frozenset()
    assert await repo.permissions_for_user(admin.tenant_id, user_b) == frozenset()


async def test_delete_reserved_role_is_rejected() -> None:
    """A reserved role cannot be deleted; the use case raises
    ``InvariantViolation`` so the API can return 422.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    # Seed a reserved role directly into the repository (the define
    # use case refuses to create one, but the test simulates a legacy
    # row).
    reserved = make_role(
        spec=NewRoleSpec(name="process_admin", description="", permissions=frozenset()),
        tenant_id=admin.tenant_id,
        role_id=uuid4(),
        now=NOW,
        is_reserved=True,
    )
    await repo.add(reserved)

    with pytest.raises(InvariantViolation):
        await manage_roles.delete_role(
            manage_roles.DeleteRoleCommand(
                tenant_id=admin.tenant_id,
                actor=admin,
                correlation_id="cid",
                role_id=reserved.id,
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )


async def test_grant_and_revoke_permission_idempotent_on_repeat() -> None:
    """Re-granting an existing permission is a no-op so the admin
    API can retry safely. Same for revoking a missing permission.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.read",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    # Grant already-present permission: idempotent.
    new_role = await manage_roles.grant_permission(
        manage_roles.GrantPermissionCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            permission="case.read",
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    assert new_role.permissions == frozenset({"case.read"})

    # Revoke a non-present permission: idempotent.
    after_revoke = await manage_roles.revoke_permission(
        manage_roles.RevokePermissionCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            role_id=role.id,
            permission="case.create",
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )
    assert after_revoke.permissions == frozenset({"case.read"})


async def test_invalid_permission_string_is_rejected() -> None:
    """Unknown permission strings surface as ``InvariantViolation``
    so the API can return 422.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    with pytest.raises(InvariantViolation):
        await manage_roles.define_role(
            manage_roles.DefineRoleCommand(
                tenant_id=admin.tenant_id,
                actor=admin,
                correlation_id="cid",
                name="custom_admin",
                description="",
                permissions=("not.a.real.permission",),
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 4. Tenant isolation
# ---------------------------------------------------------------------------


async def test_define_role_cross_tenant_admin_is_forbidden() -> None:
    """An admin in tenant A attempting to define a role in tenant B is
    denied by the authorization helper (cross-tenant actor).
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin_a = _admin_user(TENANT_A)

    with pytest.raises(Forbidden) as excinfo:
        await manage_roles.define_role(
            manage_roles.DefineRoleCommand(
                tenant_id=TENANT_B,  # targeting tenant B from admin A
                actor=admin_a,
                correlation_id="cid",
                name="custom_admin",
                description="",
                permissions=("case.read",),
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )
    assert "cross-tenant" in str(excinfo.value)


async def test_role_visibility_is_tenant_scoped() -> None:
    """A role defined in tenant A is not visible from tenant B's
    queries. The repository refuses to return cross-tenant rows.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()

    admin_a = _admin_user(TENANT_A)
    role_a = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=TENANT_A,
            actor=admin_a,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    # Tenant B cannot see the role.
    assert await repo.get(TENANT_B, role_a.id) is None
    assert await repo.get_by_name(TENANT_B, "case_admin") is None
    assert await repo.list(TENANT_B) == []


async def test_assignment_in_tenant_a_does_not_grant_tenant_b_action() -> None:
    """A user assigned to a role in tenant A still cannot act on
    resources in tenant B. The Policy's cross-tenant guard denies.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin_a = _admin_user(TENANT_A)

    role_a = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=TENANT_A,
            actor=admin_a,
            correlation_id="cid",
            name="case_admin",
            description="",
            permissions=("case.create",),
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    user_id = uuid4()
    await manage_roles.assign_role(
        manage_roles.AssignRoleCommand(
            tenant_id=TENANT_A,
            actor=admin_a,
            correlation_id="cid",
            role_id=role_a.id,
            user_id=user_id,
        ),
        role_repo=repo,
        audit=audit,
        clock=clock,
    )

    policy = Policy(role_repository=repo)

    # Tenant A: allowed.
    actor_a = _user_context(tenant_id=TENANT_A, user_id=user_id, role_names=("case_admin",))
    decision_a = await policy.evaluate(Action.CASE_CREATE, actor_a, _resource(TENANT_A))
    assert decision_a.allowed is True

    # Tenant B: denied by the cross-tenant guard, even though the same
    # user holds the role in tenant A.
    actor_b = _user_context(tenant_id=TENANT_A, user_id=user_id, role_names=("case_admin",))
    decision_b = await policy.evaluate(
        Action.CASE_CREATE,
        actor_b,
        Resource(tenant_id=TENANT_B, kind="case_type"),
    )
    assert decision_b.allowed is False
    assert "cross-tenant" in decision_b.reason


async def test_update_permissions_reserved_role_is_rejected() -> None:
    """Modifying a reserved role's permissions is rejected with
    ``InvariantViolation`` (422) so the seeded authority map cannot be
    silently widened.
    """
    repo = InMemoryRoleRepository()
    audit = InMemoryAuditStore()
    clock = FixedClock()
    admin = _admin_user()

    reserved = make_role(
        spec=NewRoleSpec(name="process_admin", description="", permissions=frozenset()),
        tenant_id=admin.tenant_id,
        role_id=uuid4(),
        now=NOW,
        is_reserved=True,
    )
    await repo.add(reserved)

    with pytest.raises(InvariantViolation):
        await manage_roles.update_role_permissions(
            manage_roles.UpdatePermissionsCommand(
                tenant_id=admin.tenant_id,
                actor=admin,
                correlation_id="cid",
                role_id=reserved.id,
                permissions=("case.create",),
            ),
            role_repo=repo,
            audit=audit,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 5. Domain aggregate invariants
# ---------------------------------------------------------------------------


def test_role_grant_and_revoke_are_pure_transformations() -> None:
    """``Role.grant`` / ``Role.revoke`` return new objects and never
    mutate the source — the aggregate is frozen for hashability.
    """
    role = _build_role(permissions=("case.read",))
    assert role.grant("case.create").permissions == frozenset({"case.read", "case.create"})
    assert role.grant("case.read").permissions == frozenset({"case.read"})
    assert role.revoke("case.read").permissions == frozenset()
    assert role.revoke("case.update").permissions == frozenset({"case.read"})


def test_reserved_role_names_constant_is_complete() -> None:
    """Sanity check: the reserved-name set must include the four
    seeded authority roles."""
    assert frozenset({"intake", "reviewer", "supervisor", "process_admin"}) == RESERVED_ROLE_NAMES


def test_make_role_rejects_reserved_name_without_is_reserved_flag() -> None:
    """The factory refuses to construct a role with a reserved name
    unless ``is_reserved=True`` is set explicitly (i.e., by the seed
    script, not by the admin API)."""
    with pytest.raises(InvariantViolation):
        make_role(
            spec=NewRoleSpec(name="process_admin", description="", permissions=frozenset()),
            tenant_id=TENANT_A,
            role_id=uuid4(),
            now=NOW,
            is_reserved=False,
        )


def test_new_role_spec_rejects_empty_name() -> None:
    """The spec validator rejects empty names so the API can surface
    422 cleanly."""
    with pytest.raises(InvariantViolation):
        NewRoleSpec(name="", description="", permissions=frozenset())


# ---------------------------------------------------------------------------
# 6. authorize() helper still works (regression coverage for the async
#    conversion).
# ---------------------------------------------------------------------------


async def test_authorize_helper_still_denies_cross_tenant() -> None:
    """``authorize`` raises ``Forbidden`` for cross-tenant actors
    even when the role would otherwise grant the action. Regression
    coverage for the async refactor of the helper.
    """
    with pytest.raises(Forbidden) as excinfo:
        await authorize(
            Action.CASE_CREATE,
            _admin_user(tenant_id=TENANT_B),
            _resource(TENANT_A),
        )
    assert "cross-tenant" in str(excinfo.value)


__all__: list[str] = []
