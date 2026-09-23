"""Pydantic request/response schemas for the role admin API.

These are wire projections of the domain ``Role`` /
``RoleAssignment`` shapes (see ``app/domain/role.py``). The HTTP
layer does not invent fields; the schema mirrors the canonical
aggregate so the OpenAPI types match the long-lived declarative
contract.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RoleSummary(BaseModel):
    """A role as returned to admins.

    Excludes the per-permission internal description; callers can
    fetch a full record by id if needed.
    """

    id: UUID
    tenant_id: UUID
    name: str
    description: str
    permissions: list[str]
    is_reserved: bool
    created_at: datetime
    updated_at: datetime


class DefineRoleRequest(BaseModel):
    """Body for ``POST /api/v1/admin/roles``."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    permissions: list[str] = Field(default_factory=list)


class UpdatePermissionsRequest(BaseModel):
    """Body for ``PUT /api/v1/admin/roles/{role_id}/permissions``."""

    permissions: list[str] = Field(default_factory=list)


class GrantPermissionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/roles/{role_id}/permissions``."""

    permission: str = Field(min_length=1, max_length=64)


class AssignRoleRequest(BaseModel):
    """Body for ``POST /api/v1/admin/roles/{role_id}/assignments``."""

    user_id: UUID


class AssignmentResponse(BaseModel):
    """Confirmation shape returned after a successful assign."""

    user_id: UUID
    role_id: UUID
    tenant_id: UUID
    assigned_at: datetime


__all__ = [
    "AssignRoleRequest",
    "AssignmentResponse",
    "DefineRoleRequest",
    "GrantPermissionRequest",
    "RoleSummary",
    "UpdatePermissionsRequest",
]
