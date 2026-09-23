"""Coexistence policy use cases (P14-US6 / FM-059).

This module owns the application layer for the truthful coexistence
model: a process administrator drafts a coexistence policy that names
its source systems, declares ownership + migration state, and (by
default) declares that real-time sync is OFF. The application layer
talks to the rest of the platform — authorization policy, audit chain,
outbox, repository — and the domain (``app.domain.coexistence``) is
the deterministic backstop.

# Truthfulness is the contract (P14 §1, §2 / Constitution V.10 / XII.5)

Every entry point refuses to do anything that would imply the platform
has a real-time channel with legacy. Specifically:

- ``define_coexistence_policy`` defaults ``real_time_sync_enabled=False``.
  Setting it to ``True`` requires the explicit authorization record; the
  domain refuses the construction without one (Constitution V.10).
- ``record_import_evidence`` appends an import-job id; it never writes
  anything that looks like a streaming cursor. The audit chain is the
  authoritative record of "legacy data flowed into this platform".

# Authorization

- **Define / read**: require ``ADMIN_TENANT_CONFIG`` (process
  administrator).
- **Record import evidence / advance migration state**: also require
  ``ADMIN_TENANT_CONFIG``. Importing is itself an operator action; the
  application surfaces the import job id via ``record_import_evidence``
  so the policy's evidence trail is one writer.

The Gated ``real_time_sync_enabled=True`` path requires BOTH
``ADMIN_TENANT_CONFIG`` AND a separate (currently non-existent)
authorization action — denied by default today. The synthetic / local
policy path uses ``real_time_sync_enabled=False`` and is the only
admissible shape in the first slice (AGENTS.md §18).

# Multi-tenant posture

Every entry point takes ``tenant_id`` from the command; cross-tenant
reads return ``None`` at the repository (Constitution V). The use case
translates ``None`` to ``NotFound`` so the API never confirms
cross-tenant existence.

# Synthetic posture

The first slice ships with synthetic source labels (e.g.
``"synthetic-legacy"``); the operator exercises the policy to verify
the audit chain. No outbound calls are made — the seam
(``app.infrastructure.connectors``) is reserved for the production
adapter (ADR-015, ADR-016).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.coexistence import (
    CoexistencePolicy,
    MigrationState,
    OwnershipRule,
    is_terminal_migration_state,
)
from app.domain.errors import Conflict, NotFound
from app.domain.errors import ValidationFailed as DomainValidationFailed

# Stable event types used by the audit + outbox trail. Strings are the
# contract; subscribers and the operator console key on them.
EVENT_COEXISTENCE_POLICY_DRAFTED = "CoexistencePolicyDrafted"
EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED = "CoexistenceImportEvidenceRecorded"
EVENT_COEXISTENCE_MIGRATION_STATE_CHANGED = "CoexistenceMigrationStateChanged"

SUBJECT_TYPE_COEXISTENCE_POLICY: Final[str] = "coexistence_policy"

# The set of source systems the first slice recognizes as **named**
# in a coexistence policy. The list is closed at construction time
# so a typo cannot silently widen the policy's vocabulary. Synthetic
# is the only label the first slice ships with (Constitution V.10).
# Adding a new label is a product decision (ADR-016) and must be
# recorded here in lockstep with authorization.
RECOGNIZED_SOURCE_SYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "synthetic-legacy",
        "onbase",
        "panzura",
        "sharepoint",
    }
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DefineCoexistencePolicyCommand:
    """The input to drafting a v1 coexistence policy."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    key: str
    source_systems: tuple[str, ...]
    ownership: OwnershipRule
    migration_state: MigrationState
    provenance_note: str = ""
    real_time_sync_enabled: bool = False
    sync_authorized_by: UUID | None = None
    sync_authorized_at: Any = None
    ownership_authorized_by: UUID | None = None
    ownership_authorized_at: Any = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RecordImportEvidenceCommand:
    """The input to appending an import-job id to the policy's evidence."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    policy_id: UUID
    import_job_id: UUID
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class SetMigrationStateCommand:
    """The input to advancing the policy's migration state."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    policy_id: UUID
    new_state: MigrationState
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class GetCoexistencePolicyQuery:
    """The input to reading a single policy."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    policy_id: UUID


@dataclass(frozen=True, slots=True)
class ListCoexistencePoliciesQuery:
    """The input to listing policies for the tenant."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    limit: int = 50
    offset: int = 0


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DefineCoexistencePolicyResult:
    policy: CoexistencePolicy


@dataclass(frozen=True, slots=True)
class RecordImportEvidenceResult:
    policy: CoexistencePolicy


@dataclass(frozen=True, slots=True)
class SetMigrationStateResult:
    policy: CoexistencePolicy


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def define_coexistence_policy(
    cmd: DefineCoexistencePolicyCommand,
    *,
    policy_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> DefineCoexistencePolicyResult:
    """Draft a fresh v1 coexistence policy for the tenant.

    Authorization: ``ADMIN_TENANT_CONFIG`` always; the Gated real-time
    sync / SHARED-ownership paths additionally require the
    corresponding authorization record (passed via the command). The
    domain refuses to construct the row without the record regardless
    of caller — this is the deterministic backstop of Constitution
    V.10 / XII.5 / P14 §1 (see ``app.domain.coexistence.__post_init__``).
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="coexistence_policy"),
    )

    # Validate source_systems are recognized. The application layer is
    # the friendly gate; the domain refuses unrecognized ownership /
    # migration_state values.
    for src in cmd.source_systems:
        if src not in RECOGNIZED_SOURCE_SYSTEMS:
            raise DomainValidationFailed(
                f"unrecognized source system {src!r}; "
                f"recognized: {sorted(RECOGNIZED_SOURCE_SYSTEMS)}",
                field="source_systems",
            )

    # Refuse duplicate keys (per-tenant uniqueness on key + version=1).
    existing = await policy_repo.get_by_key(cmd.tenant_id, cmd.key)
    if existing is not None:
        raise Conflict(f"coexistence policy with key {cmd.key!r} already exists for tenant")

    now = clock.now()
    policy = CoexistencePolicy.draft(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        source_systems=cmd.source_systems,
        ownership=cmd.ownership,
        migration_state=cmd.migration_state,
        created_by=cmd.actor_id,
        now=now,
        provenance_note=cmd.provenance_note,
        real_time_sync_enabled=cmd.real_time_sync_enabled,
        sync_authorized_by=cmd.sync_authorized_by,
        sync_authorized_at=cmd.sync_authorized_at,
        ownership_authorized_by=cmd.ownership_authorized_by,
        ownership_authorized_at=cmd.ownership_authorized_at,
    )
    await policy_repo.add(policy)
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=policy.id,
        event_type=EVENT_COEXISTENCE_POLICY_DRAFTED,
        payload={
            "key": policy.key,
            "version": policy.version,
            "source_systems": list(policy.source_systems),
            "ownership": policy.ownership.value,
            "migration_state": policy.migration_state.value,
            "real_time_sync_enabled": policy.real_time_sync_enabled,
            "provenance_note": policy.provenance_note,
            "is_terminal": is_terminal_migration_state(policy.migration_state),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_COEXISTENCE_POLICY_DRAFTED,
        event_id=uuid4(),
        payload={
            "policy_id": str(policy.id),
            "tenant_id": str(cmd.tenant_id),
            "key": policy.key,
            "version": policy.version,
            "source_systems": list(policy.source_systems),
            "ownership": policy.ownership.value,
            "migration_state": policy.migration_state.value,
            "real_time_sync_enabled": policy.real_time_sync_enabled,
            "occurred_at": now.isoformat(),
        },
    )
    return DefineCoexistencePolicyResult(policy=policy)


async def record_import_evidence(
    cmd: RecordImportEvidenceCommand,
    *,
    policy_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> RecordImportEvidenceResult:
    """Append an import-job id to the policy's evidence trail.

    The evidence trail is the authoritative audit record of "legacy
    data flowed into this platform". The migration_state is bumped to
    ``IMPORTED`` if the policy was still ``UNMIGRATED``. Idempotent on
    a duplicate import-job id — an operator retry must not produce a
    second audit row for the same evidence.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="coexistence_policy"),
    )

    policy = await _load(policy_repo, cmd.tenant_id, cmd.policy_id)
    if cmd.import_job_id in policy.sync_evidence:
        return RecordImportEvidenceResult(policy=policy)

    now = clock.now()
    updated = policy.record_import_evidence(import_job_id=cmd.import_job_id, at=now)
    await policy_repo.update(updated)
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=updated.id,
        event_type=EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED,
        payload={
            "key": updated.key,
            "version": updated.version,
            "import_job_id": str(cmd.import_job_id),
            "evidence_count": len(updated.sync_evidence),
            "migration_state": updated.migration_state.value,
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED,
        event_id=uuid4(),
        payload={
            "policy_id": str(updated.id),
            "tenant_id": str(cmd.tenant_id),
            "key": updated.key,
            "version": updated.version,
            "import_job_id": str(cmd.import_job_id),
            "migration_state": updated.migration_state.value,
            "occurred_at": now.isoformat(),
        },
    )
    return RecordImportEvidenceResult(policy=updated)


async def set_migration_state(
    cmd: SetMigrationStateCommand,
    *,
    policy_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> SetMigrationStateResult:
    """Advance the policy's migration state (operator-driven).

    Allowed transitions (see ``app.domain.coexistence.set_migration_state``):
    ``UNMIGRATED → IMPORTED → RECONCILED``,
    ``IMPORTED → DIVERGENT``,
    ``DIVERGENT → RECONCILED``,
    and the terminal ``→ RETIRED_FROM_SOURCE``. Backward transitions
    other than ``RECONCILED ↔ DIVERGENT`` are refused.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="coexistence_policy"),
    )

    policy = await _load(policy_repo, cmd.tenant_id, cmd.policy_id)
    now = clock.now()
    updated = policy.set_migration_state(new_state=cmd.new_state, at=now)
    if updated is policy:
        return SetMigrationStateResult(policy=policy)
    await policy_repo.update(updated)
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=updated.id,
        event_type=EVENT_COEXISTENCE_MIGRATION_STATE_CHANGED,
        payload={
            "key": updated.key,
            "version": updated.version,
            "from_state": policy.migration_state.value,
            "to_state": updated.migration_state.value,
            "is_terminal": is_terminal_migration_state(updated.migration_state),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_COEXISTENCE_MIGRATION_STATE_CHANGED,
        event_id=uuid4(),
        payload={
            "policy_id": str(updated.id),
            "tenant_id": str(cmd.tenant_id),
            "key": updated.key,
            "version": updated.version,
            "from_state": policy.migration_state.value,
            "to_state": updated.migration_state.value,
            "occurred_at": now.isoformat(),
        },
    )
    return SetMigrationStateResult(policy=updated)


async def get_coexistence_policy(
    query: GetCoexistencePolicyQuery,
    *,
    policy_repo: Any,
) -> CoexistencePolicy:
    """Read a single policy. Tenant-scoped; cross-tenant returns ``NotFound``."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(query),
        Resource(tenant_id=query.tenant_id, kind="coexistence_policy"),
    )
    return await _load(policy_repo, query.tenant_id, query.policy_id)


async def list_coexistence_policies(
    query: ListCoexistencePoliciesQuery,
    *,
    policy_repo: Any,
) -> list[CoexistencePolicy]:
    """List policies for the tenant, newest first."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(query),
        Resource(tenant_id=query.tenant_id, kind="coexistence_policy"),
    )
    return list(await policy_repo.list(query.tenant_id, limit=query.limit, offset=query.offset))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load(policy_repo: Any, tenant_id: UUID, policy_id: UUID) -> CoexistencePolicy:
    row = await policy_repo.get(tenant_id, policy_id)
    if row is None:
        raise NotFound(f"coexistence policy {policy_id} not found in tenant")
    return row  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _user_context(cmd: Any) -> Any:
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


async def _emit_audit(
    audit: Any,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    subject_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str,
    occurred_at: Any,
) -> None:
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_COEXISTENCE_POLICY,
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=occurred_at,
    )


__all__ = [
    "DefineCoexistencePolicyCommand",
    "DefineCoexistencePolicyResult",
    "EVENT_COEXISTENCE_IMPORT_EVIDENCE_RECORDED",
    "EVENT_COEXISTENCE_MIGRATION_STATE_CHANGED",
    "EVENT_COEXISTENCE_POLICY_DRAFTED",
    "GetCoexistencePolicyQuery",
    "ListCoexistencePoliciesQuery",
    "RECOGNIZED_SOURCE_SYSTEMS",
    "RecordImportEvidenceCommand",
    "RecordImportEvidenceResult",
    "SUBJECT_TYPE_COEXISTENCE_POLICY",
    "SetMigrationStateCommand",
    "SetMigrationStateResult",
    "define_coexistence_policy",
    "get_coexistence_policy",
    "list_coexistence_policies",
    "record_import_evidence",
    "set_migration_state",
]
