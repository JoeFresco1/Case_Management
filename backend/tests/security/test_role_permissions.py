"""Domain role/permission model tests (P11-US4 / FM-048).

Covers the pure domain kernel in ``app.domain.permissions`` plus the
``Role`` / ``RoleAssignment`` aggregate rules in ``app.domain.role``:

- Deny-by-default is preserved (empty set, unknown action deny).
- Allow when the permission is granted via an assignment.
- Revocation (assignment removed, or permission removed from the role)
  denies on the very next evaluation.
- Role-assignment rules: tenant-scoped, user-scoped, dangling rows
  ignored, union across multiple roles.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Structure is ``// given / // when / // then``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.errors import InvariantViolation
from app.domain.permissions import (
    REASON_ALLOWED,
    evaluate_permission,
    is_revoked,
    permissions_for_assignments,
)
from app.domain.role import (
    RESERVED_ROLE_NAMES,
    NewRoleSpec,
    Role,
    RoleAssignment,
    make_role,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _role(
    *,
    tenant_id: UUID = TENANT_A,
    name: str = "case_admin",
    permissions: frozenset[str] = frozenset({"case.create"}),
) -> Role:
    # given a role spec with the requested permission set
    return make_role(
        spec=NewRoleSpec(
            name=name,
            description="test role",
            permissions=permissions,
        ),
        tenant_id=tenant_id,
        role_id=uuid4(),
        now=NOW,
    )


def _assignment(*, tenant_id: UUID, user_id: UUID, role_id: UUID) -> RoleAssignment:
    return RoleAssignment(
        user_id=user_id,
        role_id=role_id,
        tenant_id=tenant_id,
        assigned_at=NOW,
        assigned_by=None,
    )


# ---------------------------------------------------------------------------
# Deny by default
# ---------------------------------------------------------------------------


def test_evaluate_permission__empty_grant_set__denies() -> None:
    """An actor with no permissions is denied every action."""
    # // given an actor with an empty permission union
    granted: frozenset[str] = frozenset()
    # // when evaluating a known action in the same tenant
    decision = evaluate_permission(
        "case.create",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    # // then deny-by-default holds
    assert decision.allowed is False
    assert "no role" in decision.reason


def test_evaluate_permission__unknown_action_string__denies() -> None:
    """An empty action string is denied even with a non-empty grant set."""
    # // given a granted set that contains a real permission
    granted = frozenset({"case.create"})
    # // when evaluating an empty action string
    decision = evaluate_permission(
        "",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    # // then the unknown-action path denies (P11 Q3: absence is deny)
    assert decision.allowed is False
    assert "unknown" in decision.reason


def test_evaluate_permission__ungranted_action__denies() -> None:
    """A known action outside the grant set is denied."""
    # // given only case.create granted
    granted = frozenset({"case.create"})
    # // when asking for case.update
    decision = evaluate_permission(
        "case.update",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    # // then deny
    assert decision.allowed is False
    assert "no role" in decision.reason


def test_evaluate_permission__cross_tenant_actor__denies() -> None:
    """Cross-tenant access is denied even when the permission is held."""
    # // given the permission is granted
    granted = frozenset({"case.create"})
    # // when actor and resource tenants differ
    decision = evaluate_permission(
        "case.create",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_B,
    )
    # // then the tenant guard denies before the grant is consulted
    assert decision.allowed is False
    assert "cross-tenant" in decision.reason


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


def test_evaluate_permission__granted_action_same_tenant__allows() -> None:
    """A granted permission in the same tenant allows."""
    # // given the permission is granted
    granted = frozenset({"case.create", "case.read"})
    # // when evaluating a held action in the same tenant
    decision = evaluate_permission(
        "case.create",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    # // then allow with the assignment reason
    assert decision.allowed is True
    assert decision.reason == REASON_ALLOWED


def test_permissions_for_assignments__single_assignment__grants_union() -> None:
    """One assignment yields exactly its role's permission set."""
    # // given a role and one assignment for the user
    role = _role(permissions=frozenset({"case.create", "case.read"}))
    user_id = uuid4()
    roles = {role.id: role}
    assignments = frozenset({_assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role.id)})
    # // when computing the union
    granted = permissions_for_assignments(roles, assignments, tenant_id=TENANT_A, user_id=user_id)
    # // then the union matches and evaluation allows
    assert granted == frozenset({"case.create", "case.read"})
    decision = evaluate_permission(
        "case.read",
        granted,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    assert decision.allowed is True


def test_permissions_for_assignments__multiple_roles__unions_permissions() -> None:
    """Holding two roles grants the union of both permission sets."""
    # // given two roles with disjoint permissions, both assigned
    role_one = _role(name="role_one", permissions=frozenset({"case.create"}))
    role_two = _role(name="role_two", permissions=frozenset({"case.update"}))
    user_id = uuid4()
    roles = {role_one.id: role_one, role_two.id: role_two}
    assignments = frozenset(
        {
            _assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role_one.id),
            _assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role_two.id),
        }
    )
    # // when computing the union
    granted = permissions_for_assignments(roles, assignments, tenant_id=TENANT_A, user_id=user_id)
    # // then both actions are allowed
    assert granted == frozenset({"case.create", "case.update"})
    for action in ("case.create", "case.update"):
        assert (
            evaluate_permission(
                action,
                granted,
                actor_tenant_id=TENANT_A,
                resource_tenant_id=TENANT_A,
            )
        ).allowed is True


# ---------------------------------------------------------------------------
# Revocation: assignment removed and permission removed
# ---------------------------------------------------------------------------


def test_permissions_for_assignments__assignment_revoked__denies_next_call() -> None:
    """Removing the assignment row denies on the next evaluation."""
    # // given an assigned role that currently allows
    role = _role(permissions=frozenset({"case.create"}))
    user_id = uuid4()
    roles = {role.id: role}
    before = permissions_for_assignments(
        roles,
        frozenset({_assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role.id)}),
        tenant_id=TENANT_A,
        user_id=user_id,
    )
    assert before == frozenset({"case.create"})
    # // when the assignment is revoked (row absent)
    after = permissions_for_assignments(roles, frozenset(), tenant_id=TENANT_A, user_id=user_id)
    # // then the next evaluation denies and the revocation is detectable
    assert after == frozenset()
    assert is_revoked("case.create", before, after) is True
    decision = evaluate_permission(
        "case.create",
        after,
        actor_tenant_id=TENANT_A,
        resource_tenant_id=TENANT_A,
    )
    assert decision.allowed is False


def test_permissions_for_assignments__permission_removed_from_role__denies() -> None:
    """Removing the permission from the role denies without touching assignments."""
    # // given a role with two permissions assigned to the user
    role = _role(permissions=frozenset({"case.create", "case.read"}))
    user_id = uuid4()
    assignment = _assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role.id)
    before = permissions_for_assignments(
        {role.id: role}, frozenset({assignment}), tenant_id=TENANT_A, user_id=user_id
    )
    assert before == frozenset({"case.create", "case.read"})
    # // when the role loses one permission (pure Role.revoke transform)
    narrowed = role.revoke("case.create")
    after = permissions_for_assignments(
        {role.id: narrowed}, frozenset({assignment}), tenant_id=TENANT_A, user_id=user_id
    )
    # // then only the removed action denies; the other still allows
    assert is_revoked("case.create", before, after) is True
    assert is_revoked("case.read", before, after) is False
    assert (
        evaluate_permission(
            "case.create",
            after,
            actor_tenant_id=TENANT_A,
            resource_tenant_id=TENANT_A,
        )
    ).allowed is False
    assert (
        evaluate_permission(
            "case.read",
            after,
            actor_tenant_id=TENANT_A,
            resource_tenant_id=TENANT_A,
        )
    ).allowed is True


def test_is_revoked__never_held_permission__is_not_revoked() -> None:
    """A permission that was never held is denied but not 'revoked'."""
    # // given snapshots that never contained the action
    before: frozenset[str] = frozenset({"case.read"})
    after: frozenset[str] = frozenset({"case.read"})
    # // when asking about an unheld action
    # // then it is not a revocation (it was already denied)
    assert is_revoked("case.create", before, after) is False


# ---------------------------------------------------------------------------
# Role-assignment rules: tenant scope, user scope, dangling rows
# ---------------------------------------------------------------------------


def test_permissions_for_assignments__other_tenant_assignment__grants_nothing() -> None:
    """An assignment row from tenant A never grants in tenant B."""
    # // given an assignment scoped to tenant A
    role = _role(tenant_id=TENANT_A)
    user_id = uuid4()
    roles = {role.id: role}
    assignments = frozenset({_assignment(tenant_id=TENANT_A, user_id=user_id, role_id=role.id)})
    # // when computing the union for tenant B
    granted = permissions_for_assignments(roles, assignments, tenant_id=TENANT_B, user_id=user_id)
    # // then nothing is granted
    assert granted == frozenset()


def test_permissions_for_assignments__other_user_assignment__grants_nothing() -> None:
    """An assignment for user X never grants user Y."""
    # // given an assignment for a different user
    role = _role()
    roles = {role.id: role}
    assignments = frozenset({_assignment(tenant_id=TENANT_A, user_id=uuid4(), role_id=role.id)})
    # // when computing the union for this user
    granted = permissions_for_assignments(roles, assignments, tenant_id=TENANT_A, user_id=uuid4())
    # // then nothing is granted (fail-closed)
    assert granted == frozenset()


def test_permissions_for_assignments__dangling_role_id__ignored() -> None:
    """An assignment referencing an unknown role id grants nothing."""
    # // given an assignment whose role id is not in the snapshot
    user_id = uuid4()
    assignments = frozenset({_assignment(tenant_id=TENANT_A, user_id=user_id, role_id=uuid4())})
    # // when computing the union against an empty role snapshot
    granted = permissions_for_assignments({}, assignments, tenant_id=TENANT_A, user_id=user_id)
    # // then the dangling row is ignored (never fail-open)
    assert granted == frozenset()


# ---------------------------------------------------------------------------
# Aggregate rules: Role grant/revoke purity, reserved names
# ---------------------------------------------------------------------------


def test_role_grant_and_revoke__are_pure__original_unchanged() -> None:
    """``Role.grant`` / ``Role.revoke`` return new values; source untouched."""
    # // given a role with one permission
    role = _role(permissions=frozenset({"case.read"}))
    # // when granting and revoking
    widened = role.grant("case.create")
    narrowed = role.revoke("case.read")
    # // then the transforms are pure and idempotent on repeat
    assert widened.permissions == frozenset({"case.read", "case.create"})
    assert narrowed.permissions == frozenset()
    assert role.permissions == frozenset({"case.read"})
    assert role.grant("case.read") is role
    assert role.revoke("case.update") is role


def test_role_grant__invalid_permission__raises() -> None:
    """Granting an empty permission string raises InvariantViolation."""
    # // given a role
    role = _role()
    # // when granting an invalid permission // then raise
    with pytest.raises(InvariantViolation):
        role.grant("")


def test_make_role__reserved_name_without_flag__raises() -> None:
    """Admin APIs cannot shadow the seeded authority map (reserved names)."""
    # // given a spec using a reserved name without the seed flag
    # // when constructing // then raise
    with pytest.raises(InvariantViolation):
        make_role(
            spec=NewRoleSpec(
                name="process_admin",
                description="",
                permissions=frozenset(),
            ),
            tenant_id=TENANT_A,
            role_id=uuid4(),
            now=NOW,
            is_reserved=False,
        )


def test_reserved_role_names__contains_seeded_authority_map() -> None:
    """The reserved set covers the four seeded first-slice roles."""
    # // given the domain constant // when read // then complete
    assert frozenset({"intake", "reviewer", "supervisor", "process_admin"}) == (RESERVED_ROLE_NAMES)


__all__: list[str] = []
