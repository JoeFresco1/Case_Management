"""Domain tests for the T5 worker-home kernel (dashboard + preferences + policy).

Covers the acceptance rule "admin policy overrides user preference":
locked keys always resolve to the admin value; allowlist violations
fall back to defaults; unknown keys/values are rejected at build time.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.domain.errors import ValidationFailed
from app.domain.worker_home import (
    DashboardConfig,
    PreferencePolicy,
    UserPreferences,
    resolve_effective_preferences,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
USER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_resolve_effective_preferences__locked_key__returns_admin_value() -> None:
    """Admin lock wins over a conflicting user value (T5 acceptance)."""
    # // given a user preference conflicting with an admin lock
    user = UserPreferences.create(user_id=USER_A, tenant_id=TENANT_A, values={"theme": "dark"})
    policy = PreferencePolicy.create(
        tenant_id=TENANT_A, locked={"theme": "light"}, updated_by=USER_A
    )
    # // when resolving // then the admin value wins and is flagged locked.
    resolved = {item.key: item for item in resolve_effective_preferences(user=user, policy=policy)}
    assert resolved["theme"].value == "light"
    assert resolved["theme"].locked is True


def test_resolve_effective_preferences__allowlist_violation__falls_back_to_default() -> None:
    """A user value outside the admin allowlist resolves to the default."""
    # // given a user value outside the permitted subset
    user = UserPreferences.create(user_id=USER_A, tenant_id=TENANT_A, values={"density": "compact"})
    policy = PreferencePolicy.create(
        tenant_id=TENANT_A, allowed={"density": ["comfortable"]}, updated_by=USER_A
    )
    # // when resolving // then the default wins, unlocked.
    resolved = {item.key: item for item in resolve_effective_preferences(user=user, policy=policy)}
    assert resolved["density"].value == "comfortable"
    assert resolved["density"].locked is False


def test_resolve_effective_preferences__no_policy__returns_user_values() -> None:
    """Without a policy the user's own values pass through."""
    # // given stored user values and no policy
    user = UserPreferences.create(user_id=USER_A, tenant_id=TENANT_A, values={"theme": "dark"})
    # // when resolving // then the user value is returned unlocked.
    resolved = {item.key: item for item in resolve_effective_preferences(user=user, policy=None)}
    assert resolved["theme"].value == "dark"
    assert resolved["theme"].locked is False
    assert len(resolved) == 7


def test_dashboard_create__unknown_widget__rejects() -> None:
    """Unknown widget keys are rejected at build time (closed vocabulary)."""
    # // given an unknown widget // when creating // then ValidationFailed.
    with pytest.raises(ValidationFailed):
        DashboardConfig.create(user_id=USER_A, tenant_id=TENANT_A, widgets=("not_a_widget",))


def test_preferences_create__unknown_key__rejects() -> None:
    """Unknown preference keys are rejected at build time."""
    # // given an unknown key // when creating // then ValidationFailed.
    with pytest.raises(ValidationFailed):
        UserPreferences.create(user_id=USER_A, tenant_id=TENANT_A, values={"not_a_key": "x"})


def test_policy_create__invalid_locked_value__rejects() -> None:
    """Admin locks with unrenderable values are rejected at build time."""
    # // given an invalid forced value // when creating // then ValidationFailed.
    with pytest.raises(ValidationFailed):
        PreferencePolicy.create(tenant_id=TENANT_A, locked={"theme": "neon"})
