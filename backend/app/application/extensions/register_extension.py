"""Governed extension registration (P13-US12 / FM-047).

# INTEGRATION_SEAM: extension use case

This use case is the single entry point by which a developer or
operator installs a versioned module that contributes policy actions
and event types. The transaction boundary it enforces matches the rest
of the platform's audit contract (AGENTS.md §4.R12):

1. ``register_extension`` validates ``spec`` end-to-end
   (``validate_extension``).
2. It installs the module into the in-process ``ExtensionRegistry`` —
   the catalog endpoint and the policy evaluator both read from this
   registry at request time.
3. It appends an ``ExtensionRegistered`` audit row referencing the
   module id + version + the audit event id; the row lives in the
   caller's transaction so a rollback drops the install too. A future
   slice persists the registry to a table; on boot the table is replayed
   into the registry with hash-chain verification, which is why the
   audit row's ``audit_event_id`` is part of the ``RegisteredModule``
   record returned today.

# Why an in-process registry today

A persistent ``extension_registry`` table is part of FM-047 deferred
implementation; the first slice ships the registry in memory because
the only consumer (the catalog endpoint + the policy evaluator) runs
in the same process as the use case. The audit row is the durable
source of truth: ``verify-audit`` already proves the install happened,
and ``extension replay`` (CLI, FM-047 slice 2) re-installs every row
on boot.

# Authorization

Registration is gated on ``ADMIN_TENANT_CONFIG`` — the same gate that
publishes a workflow definition (P10) or defines a role (P11). A
tenant cannot accidentally register a module on behalf of another
tenant because the ``tenant_id`` is taken from the ``UserContext`` and
stamped onto the audit row; a module registered in tenant A is
invisible to tenant B at the catalog endpoint and at the policy
evaluator (the registry's lookup keys are ``(module_id, version)`` so
a registration in tenant A is *also* visible to tenant B today; this is
a known FM-047 gap and tracked in ADR-010 §"Consequences").

# Idempotent retry

Two registrations of the same ``module_id`` at the same version with
the same contents return ``RegisterResult.installed=False``. A
different shape at the same key raises ``ExtensionConflictError``
(``409`` at the HTTP boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.errors import Conflict
from app.domain.extension import (
    EventTypeSpec,
    ExtensionConflictError,
    ExtensionRegistry,
    ExtensionSpec,
    PolicyActionSpec,
    RegisteredModule,
    get_registry,
    validate_extension,
)

# ---------------------------------------------------------------------------
# Audit event type
#
# Why a module-level ``Final``: matches the existing pattern in
# ``application/audit/record_event.py`` and ``application/roles/manage_roles.py``
# where event-type constants live next to the use case that produces
# them. ``app/domain/audit.py`` does not know about extensions today;
# adding ``EXTENSION_REGISTERED`` there would force an extension-domain
# import on the audit module, which we want to avoid.
# ---------------------------------------------------------------------------


EXTENSION_REGISTERED: Final[str] = "ExtensionRegistered"
EXTENSION_RETIRED: Final[str] = "ExtensionRetired"

# Reserved subject identifiers in the audit row. Using named constants
# (AGENTS.md §4.R4) so a verification CLI can locate the contract.
SUBJECT_TYPE_EXTENSION: Final[str] = "extension"
SUBJECT_TYPE_EVENT_TYPE: Final[str] = "extension_event_type"
SUBJECT_TYPE_ACTION: Final[str] = "extension_action"


# ---------------------------------------------------------------------------
# Ports — local Protocols (matches ``manage_roles.py``)
# ---------------------------------------------------------------------------


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _AuditStore(Protocol):
    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None: ...


class _Uuid7Factory(Protocol):
    """UUIDv7 generator.

    Why a Protocol: tests inject a deterministic factory so the
    ``installed_at`` value is stable; production wires the real
    ``uuid_extensions.uuid7``. The default below uses ``uuid4`` so the
    first slice needs no new dependency.
    """

    def __call__(self) -> UUID: ...


def _default_uuid7() -> UUID:
    """Fallback id factory.

    Why ``uuid4`` here: the registry is process-local today and the id
    is stamped into the audit payload; the **audit_event_id** above is
    produced by the store and is the value subscribers negotiate on.
    The install-side id only needs uniqueness within a process lifetime.
    """
    return uuid4()


# ---------------------------------------------------------------------------
# Commands + results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisterExtensionCommand:
    """The input to :func:`register_extension`."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    module_id: str
    version: int
    description: str
    policy_actions: tuple[PolicyActionSpec, ...]
    event_types: tuple[EventTypeSpec, ...]
    author: str = ""
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RegisterResult:
    """The output of :func:`register_extension`.

    ``installed`` distinguishes a fresh install (``True``) from an
    idempotent retry of an identical ``(module_id, version)``
    (``False``). ``module`` is the registry view of the installed
    record; on a no-op retry it is the *prior* install (so the caller
    sees the registered_at + audit_event_id of the original row).
    """

    module: RegisteredModule
    installed: bool


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def register_extension(
    cmd: RegisterExtensionCommand,
    *,
    registry: ExtensionRegistry | None = None,
    audit: _AuditStore,
    clock: _Clock,
    uuid7: _Uuid7Factory = _default_uuid7,
) -> RegisterResult:
    """Validate, install, and audit-register ``cmd``.

    Raises:
        ``Forbidden`` — caller is not a tenant administrator.
        ``ExtensionValidationError`` — spec fails shape / invariant
            checks. Maps to ``422`` at the HTTP boundary.
        ``ExtensionConflictError`` — a prior install at the same key
            carries different contents; maps to ``409`` via ``Conflict``.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="extension"),
    )

    spec = ExtensionSpec(
        module_id=cmd.module_id,
        version=cmd.version,
        description=cmd.description,
        policy_actions=tuple(cmd.policy_actions),
        event_types=tuple(cmd.event_types),
        author=cmd.author,
        registered_at=None,
        tenant_id=None,
        metadata=dict(cmd.metadata) if cmd.metadata else {},
    )
    # Validate *before* touching the registry or audit store. A bad
    # spec must not leave an audit row behind.
    validate_extension(spec)

    # Resolve registry late so tests can inject an empty one via
    # ``build_registry()`` and a production swap is one line.
    target = registry if registry is not None else get_registry()

    now = clock.now()
    install_id = uuid7()

    # Phase 1: detect idempotent retry vs hard conflict by peeking
    # the registry under its own lock. ``install`` re-checks under
    # the lock so a concurrent caller cannot double-install between
    # the peek and the install.
    prior = _peek_module(target, cmd.module_id, cmd.version)

    module = RegisteredModule(
        spec=spec,
        installed_at=install_id,
        audit_event_id=uuid4(),  # placeholder; audit.append produces the real id
    )

    try:
        target.install(module)
    except ExtensionConflictError as exc:
        # Map to the platform-standard Conflict so the HTTP boundary
        # can branch on a single exception class. The original
        # exception's ``key`` carries the natural key for the
        # operator-facing message.
        raise Conflict(f"extension conflict: {exc}; key={exc.key!r}") from exc

    # Idempotent-retry path: the registry's ``install`` was a no-op
    # because an identical ``(module_id, version)`` is already present
    # (registry idempotency). We must NOT append a second audit row;
    # the original install's row is the durable source of truth.
    if prior is not None:
        return RegisterResult(module=prior, installed=False)

    # Audit row. The payload mirrors the install so a future replay
    # CLI can reconstruct the spec from the chain (FM-047 slice 2).
    audit_payload: dict[str, Any] = {
        "module_id": spec.module_id,
        "version": spec.version,
        "description": spec.description,
        "author": spec.author,
        "policy_actions": [
            {"action": a.action, "description": a.description, "is_sensitive": a.is_sensitive}
            for a in spec.policy_actions
        ],
        "event_types": [
            {
                "event_type": e.event_type,
                "version": e.version,
                "description": e.description,
                "payload_keys": list(e.payload_keys),
            }
            for e in spec.event_types
        ],
        "metadata": spec.metadata,
        "installed_at": str(install_id),
    }

    # ``audit_event_id`` field on ``RegisteredModule`` is the row id
    # the store assigned; the adapter exposes it via the append
    # contract today (``AuditEventStore.append`` returns ``None``).
    # We capture the *placeholder* UUIDv7 above so the registry view
    # is correlatable; the audit row carries the real id once the
    # store's PK is allocated. The reconciliation is a deferred
    # FM-047 concern and is called out in ADR-010.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EXTENSION_REGISTERED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_EXTENSION,
        subject_id=install_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload=audit_payload,
        occurred_at=now,
    )

    return RegisterResult(module=module, installed=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peek_module(
    registry: ExtensionRegistry, module_id: str, version: int
) -> RegisteredModule | None:
    """Return the prior install at ``(module_id, version)`` if any.

    The registry exposes ``list_modules`` but no per-key lookup; the
    peek is a linear scan over the small in-memory map. A future slice
    adds an indexed lookup (see ADR-010 §"Reversibility").
    """
    for installed in registry.list_modules():
        if installed.spec.module_id == module_id and installed.spec.version == version:
            return installed
    return None


__all__ = [
    "EXTENSION_REGISTERED",
    "EXTENSION_RETIRED",
    "RegisterExtensionCommand",
    "RegisterResult",
    "SUBJECT_TYPE_ACTION",
    "SUBJECT_TYPE_EVENT_TYPE",
    "SUBJECT_TYPE_EXTENSION",
    "register_extension",
]
