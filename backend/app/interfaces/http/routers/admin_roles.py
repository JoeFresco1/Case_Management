"""Admin API for role + permission administration (P11-US4 / FM-048).

Endpoints:

- ``POST   /api/v1/admin/roles`` — define a new role.
- ``GET    /api/v1/admin/roles`` — list roles for the actor's tenant.
- ``PUT    /api/v1/admin/roles/{role_id}/permissions`` — replace the
  permission set wholesale.
- ``POST   /api/v1/admin/roles/{role_id}/permissions`` — grant one
  permission (idempotent).
- ``DELETE /api/v1/admin/roles/{role_id}/permissions/{permission}`` —
  revoke one permission (idempotent).
- ``POST   /api/v1/admin/roles/{role_id}/assignments`` — assign the
  role to a user.
- ``DELETE /api/v1/admin/roles/{role_id}/assignments/{user_id}`` —
  revoke the assignment. Revocation is immediate: the policy reads
  ``role_assignments`` on every ``evaluate`` call, so the next request
  reflects the change without any cache invalidation.
- ``DELETE /api/v1/admin/roles/{role_id}`` — delete a non-reserved
  role. Reserved roles are protected by the use case.

# Authorization

Every endpoint requires ``ADMIN_TENANT_CONFIG``; the router calls
``authorize`` before touching the repository so a failed authorization
never reaches the database. Non-admin callers receive 403
(``Forbidden`` -> 403 mapping in ``interfaces/http/errors.py``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.roles import manage_roles
from app.application.roles.http_dependencies import AdminRolesHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.interfaces.http.dependencies import get_admin_roles_dependencies
from app.interfaces.http.schemas.admin_roles import (
    AssignmentResponse,
    AssignRoleRequest,
    DefineRoleRequest,
    GrantPermissionRequest,
    RoleSummary,
    UpdatePermissionsRequest,
)

router = APIRouter(prefix="/admin/roles", tags=["admin", "roles"])


def _to_summary(role) -> RoleSummary:
    return RoleSummary(
        id=role.id,
        tenant_id=role.tenant_id,
        name=role.name,
        description=role.description,
        permissions=sorted(role.permissions),
        is_reserved=role.is_reserved,
        created_at=role.created_at,
        updated_at=getattr(role, "updated_at", role.created_at) or role.created_at,
    )


@router.post(
    "",
    response_model=RoleSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Define a new role with the given permissions",
)
async def define_role(
    body: DefineRoleRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> RoleSummary:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)

    role = await manage_roles.define_role(
        manage_roles.DefineRoleCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            name=body.name,
            description=body.description,
            permissions=tuple(body.permissions),
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_summary(role)


@router.get(
    "",
    response_model=list[RoleSummary],
    summary="List roles for the actor's tenant",
)
async def list_roles(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> list[RoleSummary]:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)
    roles = await repositories.role.list(user.tenant_id)
    return [_to_summary(r) for r in roles]


@router.put(
    "/{role_id}/permissions",
    response_model=RoleSummary,
    summary="Replace the role's permission set wholesale",
)
async def replace_role_permissions(
    role_id: UUID,
    body: UpdatePermissionsRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> RoleSummary:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)
    role = await manage_roles.update_role_permissions(
        manage_roles.UpdatePermissionsCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
            permissions=tuple(body.permissions),
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_summary(role)


@router.post(
    "/{role_id}/permissions",
    response_model=RoleSummary,
    status_code=status.HTTP_200_OK,
    summary="Grant a single permission to a role",
)
async def grant_role_permission(
    role_id: UUID,
    body: GrantPermissionRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> RoleSummary:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)
    role = await manage_roles.grant_permission(
        manage_roles.GrantPermissionCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
            permission=body.permission,
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_summary(role)


@router.delete(
    "/{role_id}/permissions/{permission}",
    response_model=RoleSummary,
    summary="Revoke a single permission from a role",
)
async def revoke_role_permission(
    role_id: UUID,
    permission: str = Path(min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> RoleSummary:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)
    role = await manage_roles.revoke_permission(
        manage_roles.RevokePermissionCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
            permission=permission,
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _to_summary(role)


@router.post(
    "/{role_id}/assignments",
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a role to a user",
)
async def assign_role(
    role_id: UUID,
    body: AssignRoleRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> AssignmentResponse:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="user", id=body.user_id),
    )

    repositories = dependencies.for_session(db)
    assignment = await manage_roles.assign_role(
        manage_roles.AssignRoleCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
            user_id=body.user_id,
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return AssignmentResponse(
        user_id=assignment.user_id,
        role_id=assignment.role_id,
        tenant_id=assignment.tenant_id,
        assigned_at=assignment.assigned_at,
    )


@router.delete(
    "/{role_id}/assignments/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a role from a user (immediate)",
)
async def revoke_role_assignment(
    role_id: UUID,
    user_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> None:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="user", id=user_id),
    )

    repositories = dependencies.for_session(db)
    await manage_roles.revoke_role(
        manage_roles.RevokeRoleCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
            user_id=user_id,
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a non-reserved role and its assignments",
)
async def delete_role(
    role_id: UUID,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminRolesHttpDependencyProvider = Depends(get_admin_roles_dependencies),
) -> None:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="role"),
    )

    repositories = dependencies.for_session(db)
    await manage_roles.delete_role(
        manage_roles.DeleteRoleCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            role_id=role_id,
        ),
        role_repo=repositories.role,
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()


__all__ = ["router"]
