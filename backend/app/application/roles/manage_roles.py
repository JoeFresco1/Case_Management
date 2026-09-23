"""Role + assignment administration use cases (P11-US4 / FM-048).

This module orchestrates the admin-side of authorization:

- :func:`define_role` — create a new role with a permission set.
- :func:`update_role_permissions` — replace the permission set of an
  existing role (revocation-by-replacement is bulk; for per-action
  add/remove use ``grant_permission`` / ``revoke_permission``).
- :func:`grant_permission` / :func:`revoke_permission` — single-action
  edits that audit the change individually.
- :func:`assign_role` / :func:`revoke_role` — manage the
  user-belongs-to-role fact.
- :func:`delete_role` — remove a non-reserved role and all its
  assignments in one transaction.

All operations require ``ADMIN_TENANT_CONFIG``; the helper
:func:`_require_admin` enforces it locally before touching the
repository so a failed authorization never reaches the database.

# Revocation is immediate

The ``RoleRepository.permissions_for_user`` lookup is the live read
path used by ``Policy``. It reads the ``role_assignments`` table on
every ``evaluate`` call; there is no caching. Deleting the assignment
row (``revoke_role``) is enough to deny on the very next request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, authorize
from app.application.authorization import Resource as AuthzResource
from app.domain.context import UserContext
from app.domain.errors import Conflict, InvariantViolation, NotFound
from app.domain.role import (
    RESERVED_ROLE_NAMES,
    NewRoleSpec,
    Role,
    RoleAssignment,
    make_role,
)

# ---------------------------------------------------------------------------
# Ports — local Protocols so this module does not import infrastructure.
# ---------------------------------------------------------------------------


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _RoleRepository(Protocol):
    async def get(self, tenant_id: UUID, role_id: UUID) -> Role | None: ...

    async def get_by_name(self, tenant_id: UUID, name: str) -> Role | None: ...

    async def list(self, tenant_id: UUID) -> list[Role]: ...

    async def add(self, role: Role) -> None: ...

    async def update_permissions(
        self, tenant_id: UUID, role_id: UUID, permissions: frozenset[str]
    ) -> None: ...

    async def delete(self, tenant_id: UUID, role_id: UUID) -> None: ...

    async def assign(
        self,
        tenant_id: UUID,
        user_id: UUID,
        role_id: UUID,
        *,
        assigned_by: UUID | None,
        assigned_at: datetime,
    ) -> None: ...

    async def revoke(self, tenant_id: UUID, user_id: UUID, role_id: UUID) -> None: ...

    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]: ...


class _AuditStore(Protocol):
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
    ) -> None: ...


# ---------------------------------------------------------------------------
# Audit event types (P11-US4). The strings are stable for downstream
# subscribers / auditors; add new event types here AND in the audit event
# catalog in ``app/domain/audit.py`` when one exists for roles.
# ---------------------------------------------------------------------------

ROLE_DEFINED = "RoleDefined"
ROLE_PERMISSIONS_UPDATED = "RolePermissionsUpdated"
ROLE_PERMISSION_GRANTED = "RolePermissionGranted"
ROLE_PERMISSION_REVOKED = "RolePermissionRevoked"
ROLE_ASSIGNED = "RoleAssigned"
ROLE_REVOKED = "RoleRevoked"
ROLE_DELETED = "RoleDeleted"


# ---------------------------------------------------------------------------
# Command shapes (frozen dataclasses; mirror the create_case command).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AdminCommand:
    tenant_id: UUID
    actor: UserContext
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DefineRoleCommand(_AdminCommand):
    name: str
    description: str
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdatePermissionsCommand(_AdminCommand):
    role_id: UUID
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GrantPermissionCommand(_AdminCommand):
    role_id: UUID
    permission: str


@dataclass(frozen=True, slots=True)
class RevokePermissionCommand(_AdminCommand):
    role_id: UUID
    permission: str


@dataclass(frozen=True, slots=True)
class AssignRoleCommand(_AdminCommand):
    role_id: UUID
    user_id: UUID


@dataclass(frozen=True, slots=True)
class RevokeRoleCommand(_AdminCommand):
    role_id: UUID
    user_id: UUID


@dataclass(frozen=True, slots=True)
class DeleteRoleCommand(_AdminCommand):
    role_id: UUID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_admin(actor: UserContext, *, resource: AuthzResource) -> None:
    """Authorize the admin operation.

    Why local helper: every use case starts with the same authorization
    call; a helper makes the pattern impossible to forget and gives one
    place to audit/log denied attempts in the future.
    """
    await authorize(Action.ADMIN_TENANT_CONFIG, actor, resource)


def _validate_permissions(values: tuple[str, ...]) -> frozenset[str]:
    """Convert and validate ``Action.value`` strings.

    Raises ``ValidationFailed``-equivalent ``InvariantViolation`` for
    unknown actions so the use case can surface 422 to the caller.
    """
    out: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise InvariantViolation(f"invalid permission: {raw!r}")
        try:
            enum_value = Action(raw)
        except ValueError as exc:
            raise InvariantViolation(
                f"unknown permission {raw!r}; must be one of {[a.value for a in Action]}"
            ) from exc
        out.add(enum_value.value)
    return frozenset(out)


async def _load_role(repo: _RoleRepository, tenant_id: UUID, role_id: UUID) -> Role:
    role = await repo.get(tenant_id, role_id)
    if role is None:
        raise NotFound(f"role {role_id} not found")
    return role


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def define_role(
    cmd: DefineRoleCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> Role:
    """Create a new role with ``cmd.permissions`` granted to it."""
    await _require_admin(cmd.actor, resource=AuthzResource(tenant_id=cmd.tenant_id, kind="role"))

    permissions = _validate_permissions(cmd.permissions)
    spec = NewRoleSpec(name=cmd.name, description=cmd.description, permissions=permissions)

    existing = await role_repo.get_by_name(cmd.tenant_id, cmd.name)
    if existing is not None:
        # Idempotent re-define with identical permissions is allowed so
        # the admin API can retry safely; a different permission set is
        # a conflict because the role identity is the name.
        if existing.permissions == permissions:
            return existing
        raise Conflict(f"role {cmd.name!r} already exists with a different permission set")

    now = clock.now()
    role = make_role(
        spec=spec,
        tenant_id=cmd.tenant_id,
        role_id=uuid4(),
        now=now,
        is_reserved=False,
    )
    await role_repo.add(role)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_DEFINED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="role",
        subject_id=role.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "name": role.name,
            "permissions": sorted(role.permissions),
            "description": role.description,
        },
        occurred_at=now,
    )
    return role


async def update_role_permissions(
    cmd: UpdatePermissionsCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> Role:
    """Replace the role's permission set wholesale."""
    await _require_admin(cmd.actor, resource=AuthzResource(tenant_id=cmd.tenant_id, kind="role"))

    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    if role.is_reserved:
        raise InvariantViolation("reserved roles cannot be modified; create a new role instead")

    permissions = _validate_permissions(cmd.permissions)
    await role_repo.update_permissions(cmd.tenant_id, cmd.role_id, permissions)

    now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_PERMISSIONS_UPDATED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="role",
        subject_id=role.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "name": role.name,
            "added": sorted(permissions - role.permissions),
            "removed": sorted(role.permissions - permissions),
        },
        occurred_at=now,
    )
    return Role(
        id=role.id,
        tenant_id=role.tenant_id,
        name=role.name,
        description=role.description,
        permissions=permissions,
        is_reserved=role.is_reserved,
        created_at=role.created_at,
    )


async def grant_permission(
    cmd: GrantPermissionCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> Role:
    """Add a single permission to a role."""
    await _require_admin(cmd.actor, resource=AuthzResource(tenant_id=cmd.tenant_id, kind="role"))

    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    if role.is_reserved:
        raise InvariantViolation("reserved roles cannot be modified; create a new role instead")
    permissions = _validate_permissions((cmd.permission,))
    (permission_value,) = permissions
    new_role = role.grant(permission_value)
    await role_repo.update_permissions(cmd.tenant_id, cmd.role_id, new_role.permissions)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_PERMISSION_GRANTED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="role",
        subject_id=role.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"name": role.name, "permission": permission_value},
        occurred_at=clock.now(),
    )
    return new_role


async def revoke_permission(
    cmd: RevokePermissionCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> Role:
    """Remove a single permission from a role."""
    await _require_admin(cmd.actor, resource=AuthzResource(tenant_id=cmd.tenant_id, kind="role"))

    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    if role.is_reserved:
        raise InvariantViolation("reserved roles cannot be modified; create a new role instead")
    permissions = _validate_permissions((cmd.permission,))
    (permission_value,) = permissions
    new_role = role.revoke(permission_value)
    await role_repo.update_permissions(cmd.tenant_id, cmd.role_id, new_role.permissions)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_PERMISSION_REVOKED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="role",
        subject_id=role.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"name": role.name, "permission": permission_value},
        occurred_at=clock.now(),
    )
    return new_role


async def assign_role(
    cmd: AssignRoleCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> RoleAssignment:
    """Grant a user the permissions bundled by ``cmd.role_id``.

    The user starts gaining the permissions on the next policy
    ``evaluate`` call (live read).
    """
    await _require_admin(
        cmd.actor,
        resource=AuthzResource(tenant_id=cmd.tenant_id, kind="user", id=cmd.user_id),
    )
    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    now = clock.now()
    await role_repo.assign(
        cmd.tenant_id,
        cmd.user_id,
        cmd.role_id,
        assigned_by=cmd.actor.user_id,
        assigned_at=now,
    )
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_ASSIGNED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="user",
        subject_id=cmd.user_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"role_id": str(cmd.role_id), "role_name": role.name},
        occurred_at=now,
    )
    return RoleAssignment(
        user_id=cmd.user_id,
        role_id=cmd.role_id,
        tenant_id=cmd.tenant_id,
        assigned_at=now,
        assigned_by=cmd.actor.user_id,
    )


async def revoke_role(
    cmd: RevokeRoleCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> None:
    """Remove the assignment. Revocation is immediate."""
    await _require_admin(
        cmd.actor,
        resource=AuthzResource(tenant_id=cmd.tenant_id, kind="user", id=cmd.user_id),
    )
    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    await role_repo.revoke(cmd.tenant_id, cmd.user_id, cmd.role_id)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_REVOKED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="user",
        subject_id=cmd.user_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"role_id": str(cmd.role_id), "role_name": role.name},
        occurred_at=clock.now(),
    )


async def delete_role(
    cmd: DeleteRoleCommand,
    *,
    role_repo: _RoleRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> None:
    """Remove a non-reserved role and every assignment of it."""
    await _require_admin(cmd.actor, resource=AuthzResource(tenant_id=cmd.tenant_id, kind="role"))
    role = await _load_role(role_repo, cmd.tenant_id, cmd.role_id)
    if role.is_reserved or role.name in RESERVED_ROLE_NAMES:
        raise InvariantViolation(f"reserved role {role.name!r} cannot be deleted")
    await role_repo.delete(cmd.tenant_id, cmd.role_id)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ROLE_DELETED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="role",
        subject_id=role.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"name": role.name, "permissions": sorted(role.permissions)},
        occurred_at=clock.now(),
    )


__all__ = [
    "AssignRoleCommand",
    "DefineRoleCommand",
    "DeleteRoleCommand",
    "GrantPermissionCommand",
    "RevokePermissionCommand",
    "RevokeRoleCommand",
    "ROLE_ASSIGNED",
    "ROLE_DEFINED",
    "ROLE_DELETED",
    "ROLE_PERMISSIONS_UPDATED",
    "ROLE_PERMISSION_GRANTED",
    "ROLE_PERMISSION_REVOKED",
    "ROLE_REVOKED",
    "UpdatePermissionsCommand",
    "assign_role",
    "define_role",
    "delete_role",
    "grant_permission",
    "revoke_permission",
    "revoke_role",
    "update_role_permissions",
]
