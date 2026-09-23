"""Extension and tenant-defined event-type registry (P13-US12 / FM-047).

# INTEGRATION_SEAM: extension registry

A module developer (today: in-process Python; tomorrow: a tenant-signed
artifact loaded from disk) registers a *versioned* extension that
contributes two things to the platform:

- **Policy actions** — additional ``Action`` enum values the policy
  evaluator recognises. Policy actions are the gate that other
  extensions and operators consume (``Action`` lives in
  ``app.application.authorization``); we add to the policy by adding to
  this registry. Every action value is namespaced
  (``ext.<module>.<verb>``) so extensions cannot collide with the
  built-in ``case.*`` / ``document.*`` etc. values.
- **Event types** — additional ``DomainEvent`` schemas that downstream
  subscribers (and internal use cases) can publish and consume. Each
  event type is itself versioned; ``find_event_spec`` is the lookup that
  the outbox dispatcher / catalog endpoint use (FM-047 moves the
  built-in ``EVENT_CATALOG`` tuple to this registry as the first
  registration).

# Why a separate module instead of overloading ``domain/events.py``

``EVENT_CATALOG`` is the **built-in** contract. Extensions are the
**tenant-attached** overlay. Mixing the two would (a) make the built-in
catalog a mutable global, (b) hide the audit trail that records who
registered what, and (c) prevent the deprecation flow (extensions retire
their own event types via a new ``retired`` registration; built-ins
retire via ADR).

# Versioning

Both ``ExtensionSpec`` and ``EventTypeSpec`` carry a ``version`` field.
Two registrations of the same ``module_id`` / ``event_type`` at the same
version are a ``Conflict`` (idempotent re-registration with identical
contents is allowed — see ``register_extension``). New versions of a
module land as a fresh ``ExtensionSpec`` whose ``module_id`` is the
same and whose ``version`` is greater; the prior version stays
installed so in-flight subscribers can drain (ADR-023 additive-only
within major; breaking changes bump major).

# PHI and secrets

Per ``plan.md:259``, ``AGENTS.md:170``, and the policy in
``domain/events.py``, payload keys are referenced by id. The validator
below reuses the same redaction needle list so a careless payload key
is rejected at registration time, not at publish time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Final
from uuid import UUID as _UUID

# ---------------------------------------------------------------------------
# Naming conventions
#
# Why enforce them: a single mis-namespaced value (e.g. ``"case.delete"``
# proposed by an extension) would either silently override the built-in
# policy or fail at runtime when the role admin tried to grant it. The
# convention is the cheap point at which the conflict is caught.
# ---------------------------------------------------------------------------

# ``ext.<module>.<verb>`` — module is lowercase alphanumeric/dash, 1-32
# chars; verb is lowercase alphanumeric/underscore, 1-32 chars.
_ACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ext\.[a-z0-9][a-z0-9-]{0,31}\.[a-z0-9][a-z0-9_]{0,31}$"
)

# Module id is the same shape minus the verb segment: ``ext.<module>``.
_MODULE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ext\.[a-z0-9][a-z0-9-]{0,31}$")

# Event type names follow the built-in convention: PascalCase dotted
# (e.g. ``TenantExt.FooBar``) so the catalog endpoint produces a uniform
# surface. We deliberately reject plain ``PascalCase`` without a
# namespace prefix — extensions must declare a tenant-visible namespace
# so deprecation can target them precisely.
_EVENT_TYPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Za-z0-9]*\.[A-Z][A-Za-z0-9]+$")

_MAX_VERSION: Final[int] = 1_000_000
_MAX_PAYLOAD_KEYS: Final[int] = 64
_MAX_DESCRIPTION: Final[int] = 1_024
_MAX_PAYLOAD_KEY_LEN: Final[int] = 64


# ---------------------------------------------------------------------------
# Redaction needle list
#
# Same needles as ``app/domain/events.py``; mirrored here so a
# registration is rejected at module-load time rather than at first
# publish. Kept as a separate constant (instead of importing) to avoid
# a domain/domain cycle; both lists MUST stay in lockstep — the
# ``tests/extensions/test_registration.py`` asserts the mirror.
# ---------------------------------------------------------------------------


_PHI_KEY_NEEDLES: Final[tuple[str, ...]] = (
    "ssn",
    "dob",
    "birth",
    "address",
    "phone",
    "email",
    "passport",
    "license",
    "tax_id",
    "phi",
    "secret",
    "password",
    "token",
)


def _key_looks_like_phi(key: str) -> bool:
    """Substring + containment check mirroring ``app.domain.events``."""
    needle = key.lower()
    return any(needle in n or n in needle for n in _PHI_KEY_NEEDLES)


# ---------------------------------------------------------------------------
# Errors
#
# Domain errors are pure strings of structured information; the HTTP
# layer maps them to structured 422 / 409 responses. We deliberately do
# NOT raise ``ValidationFailed`` here so the error surface is explicit
# about which invariant was broken — the caller can branch on the
# exception class.
# ---------------------------------------------------------------------------


class ExtensionValidationError(ValueError):
    """An extension payload failed shape or invariant validation.

    Why a dedicated subclass: callers (router, use case, future CLI) want
    to distinguish "the registration was malformed" from "the
    registration conflicted with a prior one" without inspecting the
    message text.
    """


class ExtensionConflictError(Exception):
    """A registration collided with an existing entry.

    ``module_id`` + ``version`` (or ``event_type`` + ``version``) is the
    natural key; a duplicate key with identical contents is allowed
    (idempotent retry) but the caller asks ``register_extension`` for
    the no-op result. A duplicate key with different content is a hard
    conflict.
    """

    def __init__(self, message: str, *, key: str) -> None:
        super().__init__(message)
        self.key = key


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyActionSpec:
    """A single policy action contributed by an extension.

    Attributes:
        action: Wire identifier, ``ext.<module>.<verb>``.
        description: Human-readable summary (used by ``GET /extensions``).
        is_sensitive: When True the policy must redact the action name
            in operator logs. Default False — the action value itself is
            not sensitive, but extensions that handle PHI-derived
            decisions can mark themselves sensitive.
    """

    action: str
    description: str
    is_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """A single event type contributed by an extension.

    Mirrors ``app.domain.events.EventTypeSpec`` but is owned by the
    extension registry so the built-in catalog remains a separate,
    audit-stable tuple. Wire shape is identical so ``find_event_spec``
    can return either source uniformly.
    """

    event_type: str
    version: int
    description: str
    payload_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    """A versioned extension module.

    Attributes:
        module_id: Stable identifier, ``ext.<module>``. Same value across
            versions; the version field disambiguates.
        version: Monotonic integer >= 1.
        description: Human-readable summary; surfaced in the catalog
            response so an operator can see what a module does without
            grepping the source.
        policy_actions: The new ``Action`` values the module registers.
            Empty tuple means "no new actions".
        event_types: The new ``DomainEvent`` types the module registers.
            Empty tuple means "no new event types".
        author: Free-form attribution (team, source repo). Persisted for
            audit; not interpreted.
    """

    module_id: str
    version: int
    description: str
    policy_actions: tuple[PolicyActionSpec, ...]
    event_types: tuple[EventTypeSpec, ...]
    author: str = ""
    registered_at: _UUID | None = None
    tenant_id: _UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ExtensionValidationError(message)


def _validate_action_name(action: str) -> None:
    _require(
        isinstance(action, str) and bool(_ACTION_PATTERN.match(action)),
        f"policy action {action!r} must match {_ACTION_PATTERN.pattern!r} "
        "(e.g. 'ext.my-module.create')",
    )


def _validate_module_id(module_id: str) -> None:
    _require(
        isinstance(module_id, str) and bool(_MODULE_PATTERN.match(module_id)),
        f"module_id {module_id!r} must match {_MODULE_PATTERN.pattern!r} (e.g. 'ext.my-module')",
    )


def _validate_version(version: int) -> None:
    _require(
        isinstance(version, int) and not isinstance(version, bool),
        f"version must be an int, got {type(version).__name__}",
    )
    _require(
        1 <= version <= _MAX_VERSION,
        f"version must be in [1, {_MAX_VERSION}], got {version}",
    )


def _validate_event_type_name(event_type: str) -> None:
    _require(
        isinstance(event_type, str) and bool(_EVENT_TYPE_PATTERN.match(event_type)),
        f"event_type {event_type!r} must match {_EVENT_TYPE_PATTERN.pattern!r} "
        "(e.g. 'TenantExt.FooBar')",
    )


def _validate_payload_key(key: str) -> None:
    _require(
        isinstance(key, str) and 0 < len(key) <= _MAX_PAYLOAD_KEY_LEN,
        f"payload key {key!r} must be a non-empty string <= {_MAX_PAYLOAD_KEY_LEN} chars",
    )
    _require(
        not _key_looks_like_phi(key),
        f"payload key {key!r} looks like PHI; events are "
        "minimal-payload, reference sensitive data by id",
    )


def validate_extension(spec: ExtensionSpec) -> None:
    """Validate ``spec`` end-to-end.

    Raises ``ExtensionValidationError`` on the first failed invariant.
    The validator is pure; the registry calls it before installing.
    """

    _validate_module_id(spec.module_id)
    _validate_version(spec.version)
    _require(
        isinstance(spec.description, str) and 0 < len(spec.description) <= _MAX_DESCRIPTION,
        f"description must be 1..{_MAX_DESCRIPTION} chars",
    )

    action_names: set[str] = set()
    for action in spec.policy_actions:
        _validate_action_name(action.action)
        _require(
            action.action.startswith(f"{spec.module_id}."),
            (
                f"policy action {action.action!r} must start with "
                f"the module prefix {spec.module_id!r}"
            ),
        )
        _require(
            action.action not in action_names,
            f"duplicate policy action in module: {action.action!r}",
        )
        action_names.add(action.action)
        _require(
            isinstance(action.description, str) and 0 < len(action.description) <= _MAX_DESCRIPTION,
            f"action {action.action!r} description must be 1..{_MAX_DESCRIPTION}",
        )

    event_names: set[tuple[str, int]] = set()
    for event in spec.event_types:
        _validate_event_type_name(event.event_type)
        _validate_version(event.version)
        _require(
            isinstance(event.description, str) and 0 < len(event.description) <= _MAX_DESCRIPTION,
            f"event {event.event_type!r} description must be 1..{_MAX_DESCRIPTION}",
        )
        _require(
            0 <= len(event.payload_keys) <= _MAX_PAYLOAD_KEYS,
            f"event {event.event_type!r} must declare 0..{_MAX_PAYLOAD_KEYS} payload keys",
        )
        seen_keys: set[str] = set()
        for key in event.payload_keys:
            _validate_payload_key(key)
            _require(
                key not in seen_keys,
                f"event {event.event_type!r} declares duplicate payload key {key!r}",
            )
            seen_keys.add(key)
        _require(
            (event.event_type, event.version) not in event_names,
            f"duplicate event type in module: {event.event_type} v{event.version}",
        )
        event_names.add((event.event_type, event.version))


# ---------------------------------------------------------------------------
# Publication compatibility (FM-047 / FM-055)
#
# Why a checker beside ``validate_extension``: shape validation proves
# one spec is well-formed, but only a *cross-version* check proves a
# new version does not break subscribers pinned to an older one.
# Publication is additive-only per event_type (ADR-023): a higher
# version must carry every payload key of every lower version. The
# check is pure so the registry (install path) and the use case
# (pre-install fast-fail) share it without I/O.
# ---------------------------------------------------------------------------


def is_compatible_event_update(
    existing: tuple[EventTypeSpec, ...], candidate: EventTypeSpec
) -> bool:
    """Return True when ``candidate`` can publish alongside ``existing``.

    Args:
        existing: Already-installed specs with the same ``event_type``.
        candidate: The spec proposed for publication.

    Returns:
        True iff the candidate keeps every key of every lower version
        (additive-only) and every higher version keeps every key of
        the candidate. An empty ``existing`` is always compatible.

    Failure mode:
        Never raises; version/type mismatches return False.
    """
    for spec in existing:
        if spec.event_type != candidate.event_type:
            return False
        if spec.version == candidate.version:
            if set(spec.payload_keys) != set(candidate.payload_keys):
                return False
            continue
        if candidate.version > spec.version:
            if not set(spec.payload_keys).issubset(set(candidate.payload_keys)):
                return False
        elif not set(candidate.payload_keys).issubset(set(spec.payload_keys)):
            return False
    return True


# ---------------------------------------------------------------------------
# Registry
#
# Process-local, thread-safe. The use case wraps every mutation with an
# audit row so the registry is reproducible from the audit chain even
# if the in-memory state is lost (process restart). The first slice is
# explicitly in-process; a future slice persists the registry to a
# table (``extension_registry``) and reads on boot.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredModule:
    """The registry's view of an installed extension.

    Distinct from ``ExtensionSpec`` because the registry stamps
    ``installed_at`` and tracks the audit ``event_id`` of the
    registration row so an operator can correlate the in-memory state
    with the audit chain.
    """

    spec: ExtensionSpec
    installed_at: _UUID  # UUIDv7 — sortable, time-ordered
    audit_event_id: _UUID


class ExtensionRegistry:
    """Thread-safe, in-process extension registry.

    Concurrency: the registry is mutated only from
    ``register_extension`` and ``retire_extension``. Both run inside an
    ``RLock``; reads (``list_modules``, ``find_event_spec``) acquire
    the same lock so a half-installed module never appears in a
    catalog response.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        # module_id -> highest version installed
        self._modules: dict[str, int] = {}
        # (module_id, version) -> RegisteredModule
        self._by_version: dict[tuple[str, int], RegisteredModule] = {}
        # event_type -> highest version installed
        self._event_versions: dict[str, int] = {}
        # (event_type, version) -> EventTypeSpec
        self._events: dict[tuple[str, int], EventTypeSpec] = {}
        # action -> True (presence set; the value carries no signal)
        self._actions: dict[str, PolicyActionSpec] = {}

    # ---- write paths -------------------------------------------------

    def install(self, module: RegisteredModule) -> None:
        """Install ``module`` and every action / event it contributes.

        Raises ``ExtensionConflictError`` for:

        - a ``(module_id, version)`` already installed with a *different*
          spec;
        - a policy action that collides with a *different* action spec;
        - an event type at the same version with a *different* spec.
        """
        with self._lock:
            spec = module.spec
            existing = self._by_version.get((spec.module_id, spec.version))
            if existing is not None:
                if _specs_equal(existing.spec, spec):
                    return  # idempotent retry
                raise ExtensionConflictError(
                    f"module {spec.module_id} v{spec.version} already installed",
                    key=f"{spec.module_id}@v{spec.version}",
                )
            for action in spec.policy_actions:
                other = self._actions.get(action.action)
                if other is not None and not _actions_equal(other, action):
                    raise ExtensionConflictError(
                        f"policy action {action.action!r} already "
                        "registered with a different shape",
                        key=action.action,
                    )
            for event in spec.event_types:
                key = (event.event_type, event.version)
                other_event = self._events.get(key)
                if other_event is not None and not _event_specs_equal(other_event, event):
                    raise ExtensionConflictError(
                        f"event_type {event.event_type} v{event.version} "
                        "already registered with a different shape",
                        key=f"{event.event_type}@v{event.version}",
                    )
                # Cross-version publication check: a new version must not
                # drop keys published under an older version (additive-only,
                # ADR-023). The loop collects installed siblings of the same
                # event_type; ``is_compatible_event_update`` fails loudly
                # with a named blocker instead of dead-lettering rows later.
                siblings = tuple(
                    installed
                    for (name, _v), installed in self._events.items()
                    if name == event.event_type and (name, _v) != key
                )
                if siblings and not is_compatible_event_update(siblings, event):
                    raise ExtensionConflictError(
                        f"event_type {event.event_type} v{event.version} "
                        "is not backward compatible with the published version(s); "
                        "new versions must keep every previously published payload key",
                        key=f"{event.event_type}@v{event.version}",
                    )
            self._by_version[(spec.module_id, spec.version)] = module
            self._modules[spec.module_id] = max(self._modules.get(spec.module_id, 0), spec.version)
            for action in spec.policy_actions:
                self._actions[action.action] = action
            for event in spec.event_types:
                self._events[(event.event_type, event.version)] = event
                self._event_versions[event.event_type] = max(
                    self._event_versions.get(event.event_type, 0), event.version
                )

    # ---- read paths --------------------------------------------------

    def list_modules(self) -> tuple[RegisteredModule, ...]:
        """Return every installed module, sorted by ``module_id`` for a
        stable catalog response."""
        with self._lock:
            items = sorted(
                self._by_version.values(),
                key=lambda m: (m.spec.module_id, m.spec.version),
            )
        return tuple(items)

    def list_actions(self) -> tuple[PolicyActionSpec, ...]:
        with self._lock:
            items = sorted(self._actions.values(), key=lambda a: a.action)
        return tuple(items)

    def list_event_specs(self) -> tuple[EventTypeSpec, ...]:
        """Return every event spec, sorted by ``(event_type, version)``."""
        with self._lock:
            items = sorted(self._events.values(), key=lambda e: (e.event_type, e.version))
        return tuple(items)

    def find_event_spec(
        self, event_type: str, *, version: int | None = None
    ) -> EventTypeSpec | None:
        """Resolve an event type to its current (or specified) spec."""
        with self._lock:
            if version is None:
                v = self._event_versions.get(event_type)
                if v is None:
                    return None
                return self._events.get((event_type, v))
            return self._events.get((event_type, version))

    def has_action(self, action: str) -> bool:
        with self._lock:
            return action in self._actions


# ---------------------------------------------------------------------------
# Equality helpers (idempotent retry detection)
# ---------------------------------------------------------------------------


def _actions_equal(a: PolicyActionSpec, b: PolicyActionSpec) -> bool:
    return (
        a.action == b.action and a.description == b.description and a.is_sensitive == b.is_sensitive
    )


def _event_specs_equal(a: EventTypeSpec, b: EventTypeSpec) -> bool:
    return (
        a.event_type == b.event_type
        and a.version == b.version
        and a.description == b.description
        and a.payload_keys == b.payload_keys
    )


def _specs_equal(a: ExtensionSpec, b: ExtensionSpec) -> bool:  # noqa: PLR0911 - early returns aid readability
    """Equality on the parts that matter for an idempotent retry.

    ``registered_at`` and ``tenant_id`` are caller-side annotations and
    excluded; ``metadata`` is excluded because the use case stamps it
    after install.
    """
    if a.module_id != b.module_id or a.version != b.version:
        return False
    if a.description != b.description or a.author != b.author:
        return False
    if len(a.policy_actions) != len(b.policy_actions):
        return False
    if len(a.event_types) != len(b.event_types):
        return False
    a_actions = sorted(ac.action for ac in a.policy_actions)
    b_actions = sorted(ac.action for ac in b.policy_actions)
    if a_actions != b_actions:
        return False
    for left_action, right_action in zip(
        sorted(a.policy_actions, key=lambda x: x.action),
        sorted(b.policy_actions, key=lambda x: x.action),
        strict=True,
    ):
        if not _actions_equal(left_action, right_action):
            return False
    for left_event, right_event in zip(
        sorted(a.event_types, key=lambda x: (x.event_type, x.version)),
        sorted(b.event_types, key=lambda x: (x.event_type, x.version)),
        strict=True,
    ):
        if not _event_specs_equal(left_event, right_event):
            return False
    return True


# ---------------------------------------------------------------------------
# Module-level singleton + builders
# ---------------------------------------------------------------------------


# The platform's process-local registry. The use case and the HTTP
# router both reach for the same instance; tests substitute their own
# via ``build_registry``.
_REGISTRY: Final[ExtensionRegistry] = ExtensionRegistry()


def get_registry() -> ExtensionRegistry:
    """Return the process-wide registry.

    Why a function instead of a module global reference: tests build
    a fresh registry and want ``monkeypatch.setattr`` to work; a
    function call is the documented substitution point.
    """
    return _REGISTRY


def build_registry() -> ExtensionRegistry:
    """Build a fresh, empty registry.

    Tests use this to start from a clean slate. The application / HTTP
    layers use ``get_registry``; production wires the singleton.
    """
    return ExtensionRegistry()


def iter_event_specs(*sources: Iterable[EventTypeSpec]) -> tuple[EventTypeSpec, ...]:
    """Flatten multiple sources (built-in catalog + extensions) into one
    sorted tuple.

    Kept here (not in ``domain/events.py``) because the layering rule
    forbids ``events.py`` from importing the extension module; the
    catalog endpoint composes both via this helper.
    """
    out: list[EventTypeSpec] = []
    for src in sources:
        out.extend(src)
    # Deduplicate by (event_type, version) keeping the first occurrence
    # so the built-in catalog wins over an extension with the same key
    # (defense-in-depth: the use case rejects the conflict at install).
    seen: set[tuple[str, int]] = set()
    deduped: list[EventTypeSpec] = []
    for spec in out:
        key = (spec.event_type, spec.version)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(spec)
    deduped.sort(key=lambda s: (s.event_type, s.version))
    return tuple(deduped)


__all__ = [
    "EventTypeSpec",
    "ExtensionConflictError",
    "ExtensionRegistry",
    "ExtensionSpec",
    "ExtensionValidationError",
    "PolicyActionSpec",
    "RegisteredModule",
    "build_registry",
    "get_registry",
    "is_compatible_event_update",
    "iter_event_specs",
    "validate_extension",
]
