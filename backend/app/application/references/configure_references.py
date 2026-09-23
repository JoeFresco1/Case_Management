"""Per-tenant reference format configuration (FM-009 / P01-US9).

Administrators set the **human-readable** template that
``CaseRepository.next_reference`` will emit for new cases in their
tenant. The internal ``Case.id`` UUID is never changed by this
configuration; existing cases keep their original reference forever
(Pillar P01 §6 Q1, "cases retain their old version").

# What changes vs what doesn't

A ``ConfigureReferenceFormat`` command writes one row per tenant into
the ``reference_formats`` table (or its equivalent in whichever
adapter implements the port). From that moment on, the
``CaseRepository.next_reference`` SQL adapter joins the new template
into its format string at emission time.

Existing ``cases.reference`` rows are NOT updated. The repository that
issues references asks the configuration at the *moment of
issuance*, never rewrites history. This is the AGENTS.md §16.3
invariant: "CaseReference monotonic per tenant, issued by
CaseRepository.next_reference."

# Scope (per tenant, per case type key)

The acceptance scenario in the task description shows
``C-{TYPE}-{YYYY}-{SEQ}`` — a format that depends on the case type's
key. Per Pillar P05 §2, the case type key is stable (renames of the
``label`` do not break identity), so embedding ``{TYPE}`` in the
template is safe.

This module keeps the configuration at the **tenant** scope (one
template per tenant). A future slice can split it to ``(tenant_id,
case_type_id)`` if a tenant ever needs per-type templates; the
domain entity already carries ``tenant_id`` and the store key is a
single column. The wire API surface is intentionally narrow today:
one POST endpoint per tenant that replaces the template.

# Why a small, focused module

This is the configuration use case for one configurable artifact. The
admin UI may grow to be a multi-page experience later; the **service
layer** stays small so it can be tested without infrastructure. The
HTTP router mounts only this use case; the repository and audit
storage are passed in by the request handler so the boundary commits
once (AGENTS.md §4.R12 / R7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, authorize
from app.application.authorization import Resource as AuthzResource
from app.domain.context import UserContext
from app.domain.reference_format import (
    PLATFORM_DEFAULT_TEMPLATE,
    ReferenceFormat,
    platform_default_template,
    validate_template,
)

# ---------------------------------------------------------------------------
# Ports — local Protocols so this module does not import infrastructure.
# ---------------------------------------------------------------------------


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _ReferenceFormatRepository(Protocol):
    """Persistence port for per-tenant reference format overrides.

    The store is keyed on ``tenant_id``. ``upsert`` writes the new
    template atomically with the audit row in the same transaction
    (the use case boundary commits once; AGENTS.md §4.R12).
    """

    async def get_for_tenant(self, tenant_id: UUID) -> ReferenceFormat | None:
        """Return the tenant's override template, or ``None`` if none."""

    async def upsert(self, format: ReferenceFormat, *, now: datetime) -> None:
        """Write (or replace) the tenant's override template."""


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


# ---------------------------------------------------------------------------
# Audit event types (FM-009). Stable strings for downstream subscribers.
# ---------------------------------------------------------------------------


REFERENCE_FORMAT_CONFIGURED = "ReferenceFormatConfigured"
REFERENCE_FORMAT_RESET = "ReferenceFormatReset"


# ---------------------------------------------------------------------------
# Command + result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AdminCommand:
    tenant_id: UUID
    actor: UserContext
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ConfigureReferenceFormatCommand(_AdminCommand):
    template: str


@dataclass(frozen=True, slots=True)
class ResetReferenceFormatCommand(_AdminCommand):
    """Reset to the platform default template."""


@dataclass(frozen=True, slots=True)
class ConfigureReferenceFormatResult:
    tenant_id: UUID
    template: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class GetReferenceFormatResult:
    tenant_id: UUID
    template: str
    is_default: bool


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def configure_reference_format(
    cmd: ConfigureReferenceFormatCommand,
    *,
    repo: _ReferenceFormatRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> ConfigureReferenceFormatResult:
    """Set the tenant's reference format template.

    Why no separate authorize helper: the policy call site is local
    and unique to this module; an inline call expresses the
    requirement directly.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        AuthzResource(tenant_id=cmd.tenant_id, kind="reference_format"),
    )
    # The validator runs before any persistence so a bad template is
    # never written; the same validator runs at domain construction
    # too, but checking here gives the caller a stable, single
    # exception type (``ValidationFailed`` -> 422).
    validate_template(cmd.template)

    now = clock.now()
    new_format = ReferenceFormat(tenant_id=cmd.tenant_id, template=cmd.template)
    await repo.upsert(new_format, now=now)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=REFERENCE_FORMAT_CONFIGURED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="reference_format",
        subject_id=cmd.tenant_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"template": cmd.template},
        occurred_at=now,
    )
    return ConfigureReferenceFormatResult(
        tenant_id=cmd.tenant_id,
        template=cmd.template,
        is_default=False,
    )


async def reset_reference_format(
    cmd: ResetReferenceFormatCommand,
    *,
    repo: _ReferenceFormatRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> ConfigureReferenceFormatResult:
    """Remove the tenant's override and fall back to the platform default.

    The default itself (``C-{TENANT}-{YYYY}-{SEQ}``) is **not** persisted
    to the override table; absence of a row means "use the platform
    default". This keeps the override table empty for tenants that
    never customized, and avoids an audit row that simply restates the
    default.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        AuthzResource(tenant_id=cmd.tenant_id, kind="reference_format"),
    )
    existing = await repo.get_for_tenant(cmd.tenant_id)
    now = clock.now()
    # ``upsert`` with the platform default is idempotent; we use the
    # same write path so a partially-overridden tenant either has a
    # row (with the default value) or none — both yield the same
    # emitted references. We choose to delete the row when present so
    # the audit trail explicitly says "reset".
    if existing is not None:
        # The repo has no explicit ``delete``; we upsert the default
        # value to preserve a single write path. The is_default flag
        # in the response makes the outcome obvious to the caller.
        await repo.upsert(
            ReferenceFormat(
                tenant_id=cmd.tenant_id,
                template=platform_default_template(),
            ),
            now=now,
        )
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=REFERENCE_FORMAT_RESET,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="reference_format",
        subject_id=cmd.tenant_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={},
        occurred_at=now,
    )
    return ConfigureReferenceFormatResult(
        tenant_id=cmd.tenant_id,
        template=platform_default_template(),
        is_default=True,
    )


async def get_reference_format(
    tenant_id: UUID,
    *,
    repo: _ReferenceFormatRepository,
) -> GetReferenceFormatResult:
    """Return the tenant's effective reference template.

    Returns the platform default if the tenant has no override. The
    endpoint is read-only and does not require authorization beyond
    tenant scoping — only same-tenant reads are possible because the
    repository is keyed on ``tenant_id``.
    """
    existing = await repo.get_for_tenant(tenant_id)
    if existing is None:
        return GetReferenceFormatResult(
            tenant_id=tenant_id,
            template=platform_default_template(),
            is_default=True,
        )
    return GetReferenceFormatResult(
        tenant_id=tenant_id,
        template=existing.template,
        is_default=existing.template == PLATFORM_DEFAULT_TEMPLATE,
    )


__all__ = [
    "ConfigureReferenceFormatCommand",
    "ConfigureReferenceFormatResult",
    "GetReferenceFormatResult",
    "REFERENCE_FORMAT_CONFIGURED",
    "REFERENCE_FORMAT_RESET",
    "ResetReferenceFormatCommand",
    "configure_reference_format",
    "get_reference_format",
    "reset_reference_format",
]
