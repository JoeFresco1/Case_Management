"""Pydantic schemas for the T5 worker-home API (dashboard + preferences + policy).

Reads are metadata-only (ids, widget keys, preference values); no case
payloads or secrets cross this surface.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.worker_home import (
    ALLOWED_PREFERENCE_KEYS,
    ALLOWED_WIDGETS,
    DashboardConfig,
    EffectivePreference,
    PreferencePolicy,
    UserPreferences,
)


class ShortcutEntry(BaseModel):
    """One personal layout shortcut (label + navigation target)."""

    label: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=200)


class SaveDashboardRequest(BaseModel):
    """Body for ``PUT /api/v1/home/dashboard``."""

    widgets: list[str] = Field(default_factory=list, max_length=12)
    favorites: list[UUID] = Field(default_factory=list, max_length=100)
    shortcuts: list[ShortcutEntry] = Field(default_factory=list, max_length=50)
    grouping: str = Field(default="none")
    sort: str = Field(default="recent")
    task_pane_case_id: UUID | None = None


class DashboardResponse(BaseModel):
    """The wire shape of a dashboard config."""

    user_id: UUID
    tenant_id: UUID
    widgets: list[str]
    favorites: list[UUID]
    shortcuts: list[ShortcutEntry]
    grouping: str
    sort: str
    task_pane_case_id: UUID | None


def dashboard_to_response(config: DashboardConfig) -> DashboardResponse:
    """Translate a ``DashboardConfig`` aggregate to its wire shape."""
    return DashboardResponse(
        user_id=config.user_id,
        tenant_id=config.tenant_id,
        widgets=list(config.widgets),
        favorites=list(config.favorites),
        shortcuts=[ShortcutEntry(label=label, target=target) for label, target in config.shortcuts],
        grouping=config.grouping,
        sort=config.sort,
        task_pane_case_id=config.task_pane_case_id,
    )


class SavePreferencesRequest(BaseModel):
    """Body for ``PUT /api/v1/home/preferences``."""

    values: dict[str, str] = Field(default_factory=dict)


class StoredPreferencesResponse(BaseModel):
    """Raw stored user values (pre-policy)."""

    user_id: UUID
    tenant_id: UUID
    values: dict[str, str] = Field(default_factory=dict)


class EffectivePreferenceEntry(BaseModel):
    """One resolved preference: value + admin-lock flag."""

    key: str
    value: str
    locked: bool


class EffectivePreferencesResponse(BaseModel):
    """The merged effective preferences (admin policy already applied)."""

    user_id: UUID
    tenant_id: UUID
    preferences: list[EffectivePreferenceEntry]


def effective_to_response(
    *, user_id: UUID, tenant_id: UUID, resolved: tuple[EffectivePreference, ...]
) -> EffectivePreferencesResponse:
    """Translate a resolved preference tuple to its wire shape."""
    return EffectivePreferencesResponse(
        user_id=user_id,
        tenant_id=tenant_id,
        preferences=[
            EffectivePreferenceEntry(key=item.key, value=item.value, locked=item.locked)
            for item in resolved
        ],
    )


class PublishPolicyRequest(BaseModel):
    """Body for ``PUT /api/v1/admin/preference-policy``."""

    locked: dict[str, str] = Field(default_factory=dict)
    allowed: dict[str, list[str]] = Field(default_factory=dict)


class PreferencePolicyResponse(BaseModel):
    """The wire shape of the tenant preference policy."""

    tenant_id: UUID
    locked: dict[str, str]
    allowed: dict[str, list[str]]
    updated_by: UUID | None


def policy_to_response(policy: PreferencePolicy) -> PreferencePolicyResponse:
    """Translate a ``PreferencePolicy`` aggregate to its wire shape."""
    return PreferencePolicyResponse(
        tenant_id=policy.tenant_id,
        locked=dict(policy.locked),
        allowed={key: sorted(values) for key, values in policy.allowed.items()},
        updated_by=policy.updated_by,
    )


def preferences_to_values(record: UserPreferences) -> dict[str, str]:
    """Return the raw stored user values (pre-policy) for the home view."""
    return dict(record.values)


__all__ = [
    "ALLOWED_PREFERENCE_KEYS",
    "ALLOWED_WIDGETS",
    "DashboardResponse",
    "EffectivePreferenceEntry",
    "EffectivePreferencesResponse",
    "PreferencePolicyResponse",
    "PublishPolicyRequest",
    "SaveDashboardRequest",
    "SavePreferencesRequest",
    "ShortcutEntry",
    "StoredPreferencesResponse",
    "dashboard_to_response",
    "effective_to_response",
    "policy_to_response",
    "preferences_to_values",
]
