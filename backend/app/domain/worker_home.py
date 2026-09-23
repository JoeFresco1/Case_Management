"""Worker home / dashboard config + user preferences + admin policy (T5).

# Why this module exists

``specs/002-leftovers/spec.md`` §3.T5 requires worker ergonomics rebuilt as
modern web UX (not cloned ribbons): configurable home/dashboard, favorites,
personal layout shortcuts, list grouping/sorting, task-pane equivalent
(dockable case context), recent/history, import preview, user preferences
plus an admin-enforced preference policy.

This module is the **pure-domain** kernel for that rule. It owns:

1. **DashboardConfig** — per-user home layout: widget list (pinned cases,
   queue summary, overdue tasks, recent history, import preview), favorites,
   shortcuts, grouping/sorting choices, and the dockable task-pane slot.
2. **UserPreferences** — personal knobs (theme, density, default grouping,
   page size, task-pane side, recent-history size). Values are a closed
   vocabulary so a caller cannot smuggle arbitrary keys into the store.
3. **PreferencePolicy** — tenant-wide admin enforcement: locked keys with
   forced values plus allowed-value allowlists. The policy **overrides**
   the user preference (acceptance: admin policy wins in tests).
4. :func:`resolve_effective_preferences` — the merge function: user values
   filtered through the policy. Locked keys always return the admin value
   with ``locked=True``; non-locked keys outside the allowlist fall back
   to the default with ``locked=False``.

# Why pure (no I/O)

All inputs are caller-supplied value objects; persistence and
authorization live in the application use case
(``app.application.worker_home.worker_home``). The admin override is a
deterministic local computation so tests can assert it without a DB.

# INTEGRATION_SEAM: none

Deterministic local computation over metadata the platform already
stores. No Azure seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from app.domain.errors import ValidationFailed

# Closed widget vocabulary for the home dashboard. ``import_preview`` is
# the T5 import-preview slot (dry-run summary card); the rest cover the
# worker-UX paragraph (favorites, shortcuts, grouping, task pane,
# recents). Unknown widgets are rejected at build time so a caller
# cannot inject an unrenderable card into another user's home.
ALLOWED_WIDGETS: Final[tuple[str, ...]] = (
    "pinned_cases",
    "queue_summary",
    "overdue_tasks",
    "recent_history",
    "favorites",
    "shortcuts",
    "import_preview",
    "task_pane",
)

# Closed preference keys + allowed values. ``theme``/``density`` style
# the shell; ``default_grouping``/``default_sort`` drive list
# grouping/sorting; ``task_pane_side`` docks the case-context pane;
# ``recent_limit`` bounds the recent/history tray; ``page_size`` bounds
# list fetches (kept small so the home view cannot request an
# unbounded page through preferences).
ALLOWED_PREFERENCE_KEYS: Final[tuple[str, ...]] = (
    "theme",
    "density",
    "default_grouping",
    "default_sort",
    "task_pane_side",
    "recent_limit",
    "page_size",
)

_ALLOWED_VALUES: Final[dict[str, frozenset[str]]] = {
    "theme": frozenset({"light", "dark", "system"}),
    "density": frozenset({"comfortable", "compact"}),
    "default_grouping": frozenset({"none", "queue", "priority", "status"}),
    "default_sort": frozenset({"recent", "due", "priority"}),
    "task_pane_side": frozenset({"left", "right", "hidden"}),
    "recent_limit": frozenset({"5", "10", "20"}),
    "page_size": frozenset({"25", "50", "100"}),
}

DEFAULT_PREFERENCES: Final[dict[str, str]] = {
    "theme": "system",
    "density": "comfortable",
    "default_grouping": "none",
    "default_sort": "recent",
    "task_pane_side": "right",
    "recent_limit": "10",
    "page_size": "50",
}

MAX_FAVORITES = 100
MAX_SHORTCUTS = 50
MAX_WIDGETS = 12


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """A user's configurable home layout (T5 worker UX).

    ``widgets`` is the ordered card list rendered by the home view;
    ``favorites`` are pinned case ids; ``shortcuts`` are named personal
    layout shortcuts (``(label, target)``); ``grouping``/``sort`` are the
    default list grouping/sorting; ``task_pane_case_id`` is the docked
    case-context slot (``None`` = undocked).
    """

    user_id: UUID
    tenant_id: UUID
    widgets: tuple[str, ...] = ()
    favorites: tuple[UUID, ...] = ()
    shortcuts: tuple[tuple[str, str], ...] = ()
    grouping: str = "none"
    sort: str = "recent"
    task_pane_case_id: UUID | None = None

    @staticmethod
    def create(
        *,
        user_id: UUID,
        tenant_id: UUID,
        widgets: tuple[str, ...] = (),
        favorites: tuple[UUID, ...] = (),
        shortcuts: tuple[tuple[str, str], ...] = (),
        grouping: str = "none",
        sort: str = "recent",
        task_pane_case_id: UUID | None = None,
    ) -> DashboardConfig:
        """Validate and build a dashboard config.

        Raises:
            ValidationFailed: on unknown widget, over-long lists, blank
                shortcut labels/targets, or bad grouping/sort values.
        """
        for widget in widgets:
            if widget not in ALLOWED_WIDGETS:
                raise ValidationFailed(f"unknown widget: {widget}")
        if len(widgets) > MAX_WIDGETS:
            raise ValidationFailed(f"too many widgets (max {MAX_WIDGETS})")
        if len(favorites) > MAX_FAVORITES:
            raise ValidationFailed(f"too many favorites (max {MAX_FAVORITES})")
        if len(shortcuts) > MAX_SHORTCUTS:
            raise ValidationFailed(f"too many shortcuts (max {MAX_SHORTCUTS})")
        for label, target in shortcuts:
            if not label.strip() or not target.strip():
                raise ValidationFailed("shortcut label and target must be non-blank")
            if len(label) > 80 or len(target) > 200:
                raise ValidationFailed("shortcut label/target too long")
        if grouping not in ("none", "queue", "priority", "status"):
            raise ValidationFailed(f"unknown grouping: {grouping}")
        if sort not in ("recent", "due", "priority"):
            raise ValidationFailed(f"unknown sort: {sort}")
        return DashboardConfig(
            user_id=user_id,
            tenant_id=tenant_id,
            widgets=tuple(widgets),
            favorites=tuple(favorites),
            shortcuts=tuple(shortcuts),
            grouping=grouping,
            sort=sort,
            task_pane_case_id=task_pane_case_id,
        )


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """A user's personal preference values (closed vocabulary)."""

    user_id: UUID
    tenant_id: UUID
    values: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def create(*, user_id: UUID, tenant_id: UUID, values: dict[str, str]) -> UserPreferences:
        """Validate and build user preferences.

        Raises:
            ValidationFailed: on unknown keys, disallowed values, or an
                oversized payload (values are short enumerations, not
                free text).
        """
        if len(values) > len(ALLOWED_PREFERENCE_KEYS):
            raise ValidationFailed("too many preference keys")
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            if key not in ALLOWED_PREFERENCE_KEYS:
                raise ValidationFailed(f"unknown preference key: {key}")
            allowed = _ALLOWED_VALUES[key]
            if value not in allowed:
                raise ValidationFailed(f"invalid value for {key}: {value!r}")
            cleaned[key] = value
        return UserPreferences(user_id=user_id, tenant_id=tenant_id, values=cleaned)


@dataclass(frozen=True, slots=True)
class PreferencePolicy:
    """Tenant-wide admin enforcement over preferences (T5 worker UX).

    ``locked`` maps a key to its forced admin value — the effective
    resolution always returns the admin value for locked keys. ``allowed``
    maps a key to its permitted subset — non-locked user values outside
    the subset fall back to the default. Both maps use the same closed
    vocabulary as :class:`UserPreferences` so an admin cannot lock an
    unknown key or force an unrenderable value.
    """

    tenant_id: UUID
    locked: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, frozenset[str]] = field(default_factory=dict)
    updated_by: UUID | None = None

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        locked: dict[str, str] | None = None,
        allowed: dict[str, list[str]] | None = None,
        updated_by: UUID | None = None,
    ) -> PreferencePolicy:
        """Validate and build an admin preference policy.

        Raises:
            ValidationFailed: on unknown keys, forced values outside the
                key vocabulary, or allowlists that are empty/unknown.
        """
        clean_locked: dict[str, str] = {}
        for key, value in (locked or {}).items():
            if key not in ALLOWED_PREFERENCE_KEYS:
                raise ValidationFailed(f"unknown preference key: {key}")
            if value not in _ALLOWED_VALUES[key]:
                raise ValidationFailed(f"invalid locked value for {key}: {value!r}")
            clean_locked[key] = value
        clean_allowed: dict[str, frozenset[str]] = {}
        for key, subset in (allowed or {}).items():
            if key not in ALLOWED_PREFERENCE_KEYS:
                raise ValidationFailed(f"unknown preference key: {key}")
            if not subset:
                raise ValidationFailed(f"empty allowlist for {key}")
            frozen = frozenset(subset)
            unknown = frozen - _ALLOWED_VALUES[key]
            if unknown:
                raise ValidationFailed(f"invalid allowed values for {key}: {sorted(unknown)}")
            clean_allowed[key] = frozen
        return PreferencePolicy(
            tenant_id=tenant_id, locked=clean_locked, allowed=clean_allowed, updated_by=updated_by
        )


@dataclass(frozen=True, slots=True)
class EffectivePreference:
    """One resolved preference: final value + whether the admin locks it."""

    key: str
    value: str
    locked: bool


def resolve_effective_preferences(
    *, user: UserPreferences, policy: PreferencePolicy | None
) -> tuple[EffectivePreference, ...]:
    """Merge user preferences through the admin policy (admin wins).

    Resolution order per key (``DEFAULT_PREFERENCES`` supplies the base):

    1. locked key → admin forced value, ``locked=True``;
    2. user value inside the allowlist (or no allowlist) → user value;
    3. otherwise → default value.

    The function is total (every key in ``ALLOWED_PREFERENCE_KEYS`` is
    returned exactly once) and deterministic (key order follows
    ``ALLOWED_PREFERENCE_KEYS``).
    """
    locked = policy.locked if policy is not None else {}
    allowed = policy.allowed if policy is not None else {}
    resolved: list[EffectivePreference] = []
    for key in ALLOWED_PREFERENCE_KEYS:
        if key in locked:
            # why: admin lock is authoritative — the user value is
            # ignored entirely so a stale/privileged preference can
            # never leak through the merge.
            resolved.append(EffectivePreference(key=key, value=locked[key], locked=True))
            continue
        user_value = user.values.get(key, DEFAULT_PREFERENCES[key])
        subset = allowed.get(key)
        if subset is not None and user_value not in subset:
            resolved.append(
                EffectivePreference(key=key, value=DEFAULT_PREFERENCES[key], locked=False)
            )
        else:
            resolved.append(EffectivePreference(key=key, value=user_value, locked=False))
    return tuple(resolved)


__all__ = [
    "ALLOWED_PREFERENCE_KEYS",
    "ALLOWED_WIDGETS",
    "DEFAULT_PREFERENCES",
    "MAX_FAVORITES",
    "MAX_SHORTCUTS",
    "MAX_WIDGETS",
    "DashboardConfig",
    "EffectivePreference",
    "PreferencePolicy",
    "UserPreferences",
    "resolve_effective_preferences",
]
