"""Pure domain permission evaluation (P11-US4 / FM-048).

This module is the **domain** side of role-based authorization. It is a
pure function library: no I/O, no SQLAlchemy, no FastAPI, no imports from
``app.application``. The application-layer ``Policy`` in
``app.application.authorization`` consults the live role-assignment store
and then delegates the actual allow/deny math to the helpers here, so the
deny-by-default rule has exactly one implementation to audit.

# Why a separate module from ``app.domain.role``

``role.py`` owns the ``Role`` / ``RoleAssignment`` aggregates and their
construction invariants (names, reserved words, frozen grant/revoke
transformations). This module owns the *evaluation* rules: given a set of
roles, a set of assignments, an actor, and a requested permission string,
is the action allowed? Keeping evaluation separate means the rule can be
unit-tested without constructing repositories or async harnesses.

# Role-assignment rules (P11-US4)

1. A user gains the union of ``permissions`` for every role they are
   **assigned to** in the ``role_assignments`` fact table. Carrying a
   role name in an IdP claim alone grants nothing.
2. An assignment applies only when ``assignment.tenant_id`` matches the
   requested tenant AND ``assignment.user_id`` matches the actor. A row
   from tenant A never grants access to a tenant B resource.
3. An assignment that references an unknown ``role_id`` is ignored
   (fail-closed, never fail-open on dangling rows).
4. Revocation is the absence of the assignment row (or the absence of
   the permission in the role's set). There is no tombstone, no cache,
   and no grace period: the next ``evaluate_permission`` call denies.
5. Deny-by-default: an empty permission set, an unknown action string,
   or a cross-tenant actor is always denied. The only way to allow is
   an explicit ``action in granted`` with matching tenants.

# No new tables

This task adds no persistence. The ``roles`` / ``role_assignments``
tables already exist; this module only reads in-memory snapshots of
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.role import Role, RoleAssignment

# Canonical "no permission" reason strings. The application Policy maps
# these onto ``Decision`` reasons; tests assert on them so a silent
# reason change is a visible failure.
REASON_ALLOWED = "role-assignment"
REASON_DENY_NO_GRANT = "no role grants this action"
REASON_DENY_CROSS_TENANT = "cross-tenant access"
REASON_DENY_UNKNOWN_ACTION = "unknown action"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The pure outcome of one permission check.

    Why frozen: decisions are audit facts. A caller that needs to log
    the decision must not be able to mutate it after the fact.
    """

    allowed: bool
    reason: str

    @classmethod
    def allow(cls, reason: str = REASON_ALLOWED) -> PermissionDecision:
        """Return an allow decision with the given reason."""
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> PermissionDecision:
        """Return a deny decision with the given reason."""
        return cls(allowed=False, reason=reason)


def permissions_for_assignments(
    roles: dict[UUID, Role],
    assignments: frozenset[RoleAssignment],
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> frozenset[str]:
    """Compute the live union of permission strings for one user.

    Args:
        roles: Role rows keyed by ``role.id`` (the caller's snapshot of
            the ``roles`` table for the tenant).
        assignments: Assignment facts (the caller's snapshot of the
            ``role_assignments`` table). Only rows matching both
            ``tenant_id`` and ``user_id`` contribute.
        tenant_id: The tenant the actor is acting in.
        user_id: The actor whose permissions are computed.

    Returns:
        The union of ``Role.permissions`` for every assigned role. Rows
        that reference an unknown ``role_id`` are silently ignored so a
        dangling row can never grant access (fail-closed).

    Failure mode: never raises on unknown roles; an empty assignment
    set yields ``frozenset()`` (which downstream evaluation denies).
    """
    granted: set[str] = set()
    for assignment in assignments:
        # Why both checks: the assignment fact carries its own tenant
        # scope, so a leaked row from tenant A cannot grant in tenant B
        # even if the caller forgot to filter (Constitution V).
        if assignment.tenant_id != tenant_id:
            continue
        if assignment.user_id != user_id:
            continue
        role = roles.get(assignment.role_id)
        if role is None:
            continue
        # Why no tenant check on the role row itself: the assignment is
        # the authority; a role row from another tenant cannot be
        # reached because its id is only resolvable through the
        # tenant-scoped ``roles`` snapshot the caller supplies.
        granted.update(role.permissions)
    return frozenset(granted)


def evaluate_permission(
    action: str,
    granted_permissions: frozenset[str],
    *,
    actor_tenant_id: UUID,
    resource_tenant_id: UUID,
) -> PermissionDecision:
    """Decide one action against a precomputed permission set.

    This is the pure deny-by-default kernel. It performs no I/O and no
    clock reads, so it is deterministic and trivially testable.

    Args:
        action: The requested permission string (e.g. ``"case.create"``).
            Must be non-empty; an empty/whitespace string is denied as
            an unknown action.
        granted_permissions: The actor's live union (see
            :func:`permissions_for_assignments`).
        actor_tenant_id: The tenant the actor is acting in.
        resource_tenant_id: The tenant that owns the target resource.

    Returns:
        ``PermissionDecision.allow`` only when the tenants match AND
        ``action`` is a member of ``granted_permissions``. Every other
        path denies.

    Failure mode: unknown action -> deny; cross-tenant -> deny; no
    matching grant -> deny. The function never raises for unknown
    inputs; it denies (fail-closed).
    """
    if not action or not action.strip():
        # Why: an empty action string has no published binding (P11 Q3:
        # absence of policy is deny), so it must not reach the set test.
        return PermissionDecision.deny(REASON_DENY_UNKNOWN_ACTION)
    if actor_tenant_id != resource_tenant_id:
        # Why first: cross-tenant is denied unconditionally, even when
        # the permission set would otherwise allow. This is
        # defense-in-depth on top of ``WHERE tenant_id = :tenant_id``.
        return PermissionDecision.deny(REASON_DENY_CROSS_TENANT)
    if action in granted_permissions:
        return PermissionDecision.allow(REASON_ALLOWED)
    return PermissionDecision.deny(REASON_DENY_NO_GRANT)


def is_revoked(
    action: str,
    granted_before: frozenset[str],
    granted_after: frozenset[str],
) -> bool:
    """Report whether ``action`` was revoked between two snapshots.

    Args:
        action: The permission string to test.
        granted_before: Union before the admin change.
        granted_after: Union after the admin change.

    Returns:
        True only when the action was present before and absent after.
        A permission that was never held is not "revoked" (it was
        already denied), and a permission still held is not revoked.

    Failure mode: pure set math; never raises.
    """
    return action in granted_before and action not in granted_after


__all__ = [
    "REASON_ALLOWED",
    "REASON_DENY_CROSS_TENANT",
    "REASON_DENY_NO_GRANT",
    "REASON_DENY_UNKNOWN_ACTION",
    "PermissionDecision",
    "evaluate_permission",
    "is_revoked",
    "permissions_for_assignments",
]
