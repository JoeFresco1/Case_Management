"""Role aggregate (P11-US4 / FM-048).

A ``Role`` is a named bundle of permission grants. Roles are the unit of
authorization an administrator publishes; users gain the union of
permissions for every role they hold.

This module is pure domain: no I/O, no SQLAlchemy, no FastAPI, no
imports from ``app.application``. The persistence shape lives in
``app.infrastructure.persistence.models``; the use case that orchestrates
create / assign / revoke lives in ``app.application.roles.manage_roles``
and is responsible for validating that each permission is a real
``Action`` enum value before persisting it.

# Why a separate aggregate from ``User``

``User.roles`` (the JSON column on ``users``) seeds local-dev identity
in ``app/deps.py`` from a static lookup; production identities come from
the IdP claim. The authoritative source for ``process_admin`` and other
admin-only grants is the ``role_assignments`` table managed here. The
``Policy`` consults the assignment store at every ``evaluate`` call so
revocation is immediate (P11-US4 acceptance).

# Why the domain stores permission strings, not enum values

The ``Action`` enum lives in ``app.application.authorization``. Importing
it here would violate the layer rule
(``app.domain`` must not import from ``app.application``). The
application use case validates every member of ``permissions`` is an
``Action`` before constructing the aggregate, and the policy converts
back into enum values when reading. The on-the-wire string is the same
``Action.value`` ("case.create" etc.) the policy already accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.domain.errors import InvariantViolation

# Reserved role names. The default seeded roles are not deletable from
# the API because they are the baseline authority map the rest of the
# application assumes (P11 §7 first-slice authority table).
RESERVED_ROLE_NAMES: frozenset[str] = frozenset(
    {"intake", "reviewer", "supervisor", "process_admin"}
)


@dataclass(frozen=True, slots=True)
class Role:
    """A named bundle of permissions.

    Why frozen: the role's permission set is published and audited; an
    in-place mutation would silently change authorization. The aggregate
    exposes ``grant`` / ``revoke`` factory methods that return a new
    value rather than mutate in place.

    ``permissions`` carries the canonical string forms ("case.create").
    The policy converts back to the ``Action`` enum when evaluating;
    persistence stores the same strings.
    """

    id: UUID
    tenant_id: UUID
    name: str
    description: str
    permissions: frozenset[str]
    is_reserved: bool = False
    created_at: Any = None  # datetime; typed at the boundary

    def grant(self, action_value: str) -> Role:
        """Return a copy with ``action_value`` added to the permission set.

        Adding a permission the role already has is a no-op (idempotent
        publish/republish semantics).
        """
        if not action_value or not isinstance(action_value, str):
            raise InvariantViolation(f"invalid permission: {action_value!r}")
        if action_value in self.permissions:
            return self
        return Role(
            id=self.id,
            tenant_id=self.tenant_id,
            name=self.name,
            description=self.description,
            permissions=self.permissions | {action_value},
            is_reserved=self.is_reserved,
            created_at=self.created_at,
        )

    def revoke(self, action_value: str) -> Role:
        """Return a copy with ``action_value`` removed from the permission set.

        Revoking a permission the role does not have is a no-op
        (idempotent). Revoking a permission from a reserved role is
        forbidden by ``manage_roles``; the domain does not enforce it
        here so this method stays a pure transformation.
        """
        if not action_value or not isinstance(action_value, str):
            raise InvariantViolation(f"invalid permission: {action_value!r}")
        if action_value not in self.permissions:
            return self
        return Role(
            id=self.id,
            tenant_id=self.tenant_id,
            name=self.name,
            description=self.description,
            permissions=self.permissions - {action_value},
            is_reserved=self.is_reserved,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """The fact that a user holds a role inside a tenant.

    The ``user_id`` references the platform's identity (local seed or
    IdP subject). The ``role_id`` is the row id of the ``roles`` table.
    """

    user_id: UUID
    role_id: UUID
    tenant_id: UUID
    assigned_at: Any = None  # datetime; typed at the boundary
    assigned_by: UUID | None = None


# ---------------------------------------------------------------------------
# Use-case inputs (kept here so the domain stays self-contained; the
# application use case imports these and adds the orchestration).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewRoleSpec:
    """The input shape for creating a new role.

    The application use case turns this into a ``Role`` with a freshly
    minted id and a frozen permission set. The ``permissions`` carry the
    canonical ``Action.value`` strings (e.g., "case.create"); the use
    case validates them against the ``Action`` enum before constructing.
    """

    name: str
    description: str
    permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise InvariantViolation("role name is required")
        if len(self.name) > 64:
            raise InvariantViolation("role name must be <= 64 characters")
        for action_value in self.permissions:
            if not isinstance(action_value, str) or not action_value:
                raise InvariantViolation(f"invalid permission: {action_value!r}")


def make_role(
    *,
    spec: NewRoleSpec,
    tenant_id: UUID,
    role_id: UUID,
    now: Any,
    is_reserved: bool = False,
) -> Role:
    """Factory for a new ``Role``.

    Why a factory: callers (seed scripts, use cases) need a consistent
    way to construct a role with the same shape. The factory rejects
    reserved-name collisions when ``is_reserved=False`` so admin APIs
    cannot silently shadow the seeded authority map.
    """
    if spec.name in RESERVED_ROLE_NAMES and not is_reserved:
        # Allow extending reserved names only when the system is
        # explicitly seeding them; refuse collisions from admin APIs.
        raise InvariantViolation(f"role name {spec.name!r} is reserved; use a different name")
    return Role(
        id=role_id,
        tenant_id=tenant_id,
        name=spec.name,
        description=spec.description,
        permissions=spec.permissions,
        is_reserved=is_reserved,
        created_at=now,
    )


__all__ = [
    "NewRoleSpec",
    "RESERVED_ROLE_NAMES",
    "Role",
    "RoleAssignment",
    "make_role",
]
