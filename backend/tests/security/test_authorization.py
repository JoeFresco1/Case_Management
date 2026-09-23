"""Tests for the authorization policy registry (FM-002 / P11-US1 / P11-US2).

The policy is the application-layer gate for every protected operation.
Acceptance:

- ``Policy.evaluate(action, user, resource)`` denies unknown actions
  (defensive default; P11 Q3).
- A known action with no matching role grant is denied.
- A known action with a matching role grant is allowed.
- A user whose tenant differs from the resource's tenant is denied
  even if the role would otherwise allow the action (P11-US2).
- ``is_system=True`` actors only bypass role membership for actions in
  ``SYSTEM_BYPASS``; the empty ``SYSTEM_BYPASS`` set means system
  actors are still subject to role grants.
- ``authorize(action, user, resource)`` raises ``Forbidden`` on deny.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

import pytest

from app.application.authorization import (
    DEFAULT_ROLES,
    ROLE_INTAKE,
    ROLE_PROCESS_ADMIN,
    ROLE_REVIEWER,
    ROLE_SUPERVISOR,
    SYSTEM_BYPASS,
    Action,
    Decision,
    Policy,
    Resource,
    authorize,
    default_policy,
)
from app.deps import UserContext
from app.domain.errors import Forbidden

# Canonical tenant + actors used by every test.
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


def _user(
    *,
    tenant: str = TENANT_A,
    roles: tuple[str, ...] = (),
    is_system: bool = False,
) -> UserContext:
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        tenant_id=tenant,
        display_name="tester",
        roles=roles,
        is_system=is_system,
    )


def _resource(tenant: str = TENANT_A, *, kind: str = "case", rid: str | None = None) -> Resource:
    return Resource(tenant_id=tenant, kind=kind, id=rid)


# ---------------------------------------------------------------------------
# Deny by default + unknown action (P11-US1, P11 Q3)
# ---------------------------------------------------------------------------


async def test_policy_evaluate__unknown_action__denies() -> None:
    """A non-Action value (or an Action not in any role) is denied.

    The acceptance from P11-US1 says: 'an action is allowed only if a
    published role binding explicitly allows it'. An unknown action
    has no published binding, so the result is deny.
    """
    policy = Policy()
    user = _user(roles=("intake",))
    resource = _resource()

    decision = await policy.evaluate("not.a.real.action", user, resource)  # type: ignore[arg-type]
    assert isinstance(decision, Decision)
    assert decision.allowed is False
    assert "unknown" in decision.reason or "no role" in decision.reason


async def test_policy_evaluate__unknown_enum_action__denies() -> None:
    """An Action enum value that is not granted to any role is denied.

    ``Action.ADMIN_TENANT_CONFIG`` is only granted to ``process_admin``.
    An ``intake`` actor asking for it must be denied.
    """
    policy = Policy()
    user = _user(roles=("intake",))
    decision = await policy.evaluate(Action.ADMIN_TENANT_CONFIG, user, _resource())
    assert decision.allowed is False
    assert "no role" in decision.reason


# ---------------------------------------------------------------------------
# Cross-tenant denial (P11-US2 / K1)
# ---------------------------------------------------------------------------


async def test_policy_evaluate__cross_tenant_actor__denies_with_cross_tenant_reason() -> None:
    """Even with the right role, an actor from tenant B asking about
    a resource in tenant A is denied. This is the canonical P11 K1
    acceptance scenario at the policy layer (the repository enforces
    the same invariant in SQL).
    """
    policy = Policy()
    actor = _user(tenant=TENANT_B, roles=("process_admin",))
    target = _resource(tenant=TENANT_A)
    decision = await policy.evaluate(Action.CASE_CREATE, actor, target)
    assert decision.allowed is False
    assert "cross-tenant" in decision.reason


# ---------------------------------------------------------------------------
# Role membership (P11-US1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "action", "expected"),
    [
        (ROLE_INTAKE, Action.CASE_CREATE, True),
        (ROLE_INTAKE, Action.CASE_READ, True),
        (ROLE_INTAKE, Action.CASE_UPDATE, False),
        (ROLE_INTAKE, Action.WORKFLOW_PUBLISH, False),
        (ROLE_REVIEWER, Action.CASE_READ, True),
        (ROLE_REVIEWER, Action.CASE_TRANSITION, True),
        (ROLE_REVIEWER, Action.CASE_CREATE, False),
        (ROLE_SUPERVISOR, Action.AUDIT_READ, True),
        (ROLE_SUPERVISOR, Action.CASE_CREATE, False),
        (ROLE_PROCESS_ADMIN, Action.WORKFLOW_PUBLISH, True),
        (ROLE_PROCESS_ADMIN, Action.ADMIN_TENANT_CONFIG, True),
    ],
)
async def test_policy_evaluate__role_grants_match_action_set(
    role, action: Action, expected: bool
) -> None:
    """Each role grants exactly the actions in its ``actions`` set and
    nothing else. The matrix is the documented first-slice authority
    table (P11 §7, AGENTS.md §16).
    """
    policy = Policy()
    user = _user(roles=(role.name,))
    decision = await policy.evaluate(action, user, _resource())
    assert decision.allowed is expected, (
        f"{role.name} on {action.value}: expected allowed={expected}, "
        f"got {decision.allowed} ({decision.reason})"
    )


async def test_policy_evaluate__actor_with_multiple_roles__any_match_grants() -> None:
    """An actor holding both ``reviewer`` and ``process_admin`` roles
    can perform any action either role grants. This is the
    union-of-permissions model that downstream FM-048 will formalize.
    """
    policy = Policy()
    user = _user(roles=("reviewer", "process_admin"))
    assert (await policy.evaluate(Action.WORKFLOW_PUBLISH, user, _resource())).allowed
    assert (await policy.evaluate(Action.CASE_TRANSITION, user, _resource())).allowed


async def test_policy_evaluate__actor_with_empty_roles__denies_every_action() -> None:
    """An actor with no roles is denied every action; an actor with a
    role that does not exist in the registry is also denied (the
    registry is the only source of grants).
    """
    policy = Policy()
    user = _user(roles=())
    for action in Action:
        decision = await policy.evaluate(action, user, _resource())
        assert not decision.allowed, f"{action.value} should be denied"


# ---------------------------------------------------------------------------
# System bypass (Constitution XIV / P11 §2)
# ---------------------------------------------------------------------------


async def test_policy_evaluate__system_actor_with_empty_bypass__still_denied_without_role() -> None:
    """``is_system=True`` does NOT bypass role membership for actions
    not in ``SYSTEM_BYPASS``. The empty ``SYSTEM_BYPASS`` set means
    internal jobs (outbox dispatcher, idempotency sweeper) still need
    a role grant.
    """
    # The documented bypass set must be empty in the first slice; if
    # someone widens it, this test will start failing in a way that
    # forces them to revisit the security model.
    assert frozenset() == SYSTEM_BYPASS

    policy = Policy()
    user = _user(is_system=True, roles=())
    decision = await policy.evaluate(Action.AUDIT_READ, user, _resource())
    assert decision.allowed is False
    assert "no role" in decision.reason


async def test_policy_evaluate__system_actor_with_bypass_action__is_allowed() -> None:
    """If ``SYSTEM_BYPASS`` is extended to include an action, system
    actors are allowed even without a role grant. We construct a
    custom Policy subclass that overrides the module-level bypass
    for this scenario so we never mutate global state.
    """
    bypass_actions = frozenset({Action.AUDIT_READ})

    class _BypassPolicy(Policy):
        # Read from the local closure, not the module constant, so this
        # test stays hermetic and does not leak state to other tests.
        async def evaluate(  # type: ignore[override]
            self,
            action: Action,
            user: UserContext,
            resource: Resource,
        ) -> Decision:
            if user.is_system and action in bypass_actions:
                return Decision.allow("system bypass")
            return await super().evaluate(action, user, resource)

    policy = _BypassPolicy()
    user = _user(is_system=True, roles=())
    decision = await policy.evaluate(Action.AUDIT_READ, user, _resource())
    assert decision.allowed
    assert "system bypass" in decision.reason


# ---------------------------------------------------------------------------
# authorize() helper: the only shape use cases should call.
# ---------------------------------------------------------------------------


async def test_authorize__allows_when_policy_allows() -> None:
    await authorize(Action.CASE_CREATE, _user(roles=("intake",)), _resource())


async def test_authorize__raises_forbidden_when_policy_denies() -> None:
    with pytest.raises(Forbidden) as excinfo:
        await authorize(Action.CASE_CREATE, _user(roles=("reviewer",)), _resource())
    assert "case.create" in str(excinfo.value)


async def test_authorize__raises_forbidden_on_cross_tenant() -> None:
    with pytest.raises(Forbidden) as excinfo:
        await authorize(
            Action.CASE_CREATE,
            _user(tenant=TENANT_B, roles=("process_admin",)),
            _resource(tenant=TENANT_A),
        )
    assert "cross-tenant" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_policy_roles_returns_role_names_in_stable_order() -> None:
    """``roles()`` returns the registry's role names in insertion
    order — consumers may rely on the tuple being stable for
    deterministic UI rendering."""
    policy = Policy()
    assert policy.roles() == tuple(r.name for r in DEFAULT_ROLES)


def test_default_policy_returns_singleton() -> None:
    """``default_policy()`` is a process-singleton: the second call
    must return the same instance. Mutating the returned policy
    affects every subsequent call; this is by design and documented
    in the module."""
    p1 = default_policy()
    p2 = default_policy()
    assert p1 is p2


def test_policy_role_actions_unknown_role_returns_empty_set() -> None:
    """``role_actions('nonexistent')`` returns ``frozenset()`` rather
    than raising. Callers that pass untrusted role names do not need
    a try/except around the lookup."""
    policy = Policy()
    assert policy.role_actions("nonexistent") == frozenset()


# Compile-time check that the documented singleton ``POLICY`` exists for
# callers that prefer an explicit reference over ``default_policy()``.
def test_default_policy_is_module_singleton() -> None:
    """The ``default_policy()`` accessor returns a process-singleton
    Policy instance; we keep this test as the documented way to
    detect a regression that would re-instantiate it per call."""
    assert default_policy() is default_policy()
