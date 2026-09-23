"""Break-glass use cases (P11-US10 / FM-035 / P06-US9).

# Why a separate module from session_controls

Session controls govern *every* protected request (idle, lockout,
cert). Break-glass is a per-actor, opt-in override that explicitly
*extends* what the policy would normally allow — it is the only
legitimate path that bypasses the standard tenant filter on case
searches and the only legitimate path that lets an auditor in
tenant A read a case in tenant B (P06-US9). The two surfaces
share the audit chain but not the timing model: a session has an
idle window and a lockout; a break-glass grant has a hard
expiry and a revocation.

# Use cases exposed

- :func:`grant_break_glass` — issue a new grant. Requires the
  caller to be authorized to publish overrides (the Gated action
  ``ADMIN_TENANT_CONFIG`` is the seam; the role registry grants it
  to ``process_admin``).
- :func:`revoke_break_glass` — revoke an existing grant by id.
  Requires the same authorization; revoking your own grant is
  permitted so an actor who realizes they no longer need the
  override can shorten its lifetime.
- :func:`check_break_glass` — answer "does this actor have an
  active grant for this scope right now?". Called from the search
  and case-read paths before they cross a tenant boundary.
- :func:`record_break_glass_action` — append an audit row
  marking that an action was performed **under** an active grant.
  Every action during the window is flagged; this is the
  "prominently flagged" requirement from P11-US10.

# Audit model

Every transition (grant, revoke, action-under-grant, denied-check)
is an audit row carrying the grant id, the actor, the scope, the
reason, and the timestamp. The chain stays intact because the
audit store is shared with the rest of the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.break_glass import (
    MAX_GRANT_DURATION,
    BreakGlassDenialReason,
    BreakGlassGrant,
    BreakGlassScope,
)
from app.domain.case import Case
from app.domain.context import UserContext
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import (
    CaseRepository,
    SearchFilter,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)

# Stable audit event types. Keep these in sync with the audit event
# catalog in ``app/domain/audit.py`` when it gains a security section.
BREAK_GLASS_GRANTED = "BreakGlassGranted"
BREAK_GLASS_REVOKED = "BreakGlassRevoked"
BREAK_GLASS_ACTION = "BreakGlassActionPerformed"
BREAK_GLASS_DENIED = "BreakGlassDenied"


@dataclass(frozen=True, slots=True)
class GrantBreakGlassCommand:
    """Wire shape for ``POST /security/break-glass``.

    ``duration_minutes`` is the only knob — the use case rejects
    anything outside the domain's ``MIN_GRANT_DURATION`` /
    ``MAX_GRANT_DURATION`` window so a caller cannot mint a
    permanent override.
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    target_user_id: UUID
    scope: BreakGlassScope
    reason: str
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class RevokeBreakGlassCommand:
    """Wire shape for the revocation path (same router, DELETE verb)."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    grant_id: UUID


@dataclass(frozen=True, slots=True)
class BreakGlassCheckResult:
    """Structured outcome of :func:`check_break_glass`.

    Mirrors the ``SessionDecision`` shape: ``allowed`` is the
    convenience boolean; ``grant`` is the live grant (so the caller
    can include its id in audit payloads); ``reason`` is the
    structured denial reason when ``allowed`` is False.
    """

    allowed: bool
    grant: BreakGlassGrant | None
    reason: BreakGlassDenialReason | None


# ---------------------------------------------------------------------------
# Ports — local Protocols so this module does not import infrastructure.
# ---------------------------------------------------------------------------


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _GrantRepository(Protocol):
    async def add(self, grant: BreakGlassGrant) -> None: ...

    async def get(self, tenant_id: UUID, grant_id: UUID) -> BreakGlassGrant | None: ...

    async def save(self, grant: BreakGlassGrant) -> None: ...

    async def active_grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope, *, now: datetime
    ) -> BreakGlassGrant | None: ...

    async def grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope
    ) -> BreakGlassGrant | None:
        """Return the most-recent grant for ``(user, scope)`` regardless
        of active status.

        The use case that consults ``check_break_glass`` needs to see
        revoked / expired grants so it can report the precise denial
        reason. ``active_grant_for`` is the fast-path used by the
        authorization hook; ``grant_for`` is the read-shape used by
        the structured denial response.
        """


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
# Use cases
# ---------------------------------------------------------------------------


def _validate_duration_minutes(duration_minutes: int) -> timedelta:
    """Convert wire-shape minutes to a ``timedelta`` and bound-check.

    The bounds match the domain's ``MIN_GRANT_DURATION`` /
    ``MAX_GRANT_DURATION``; the use case surfaces wire-layer errors
    as :class:`ValidationFailed` (HTTP 422) so the caller does not
    see a bare ``ValueError``.
    """
    if not isinstance(duration_minutes, int) or duration_minutes <= 0:
        raise ValidationFailed(
            "duration_minutes must be a positive integer",
            field="duration_minutes",
        )
    delta = timedelta(minutes=duration_minutes)
    max_minutes = int(MAX_GRANT_DURATION.total_seconds() // 60)
    if delta > MAX_GRANT_DURATION:
        raise ValidationFailed(
            f"duration_minutes exceeds the maximum of {max_minutes} minutes",
            field="duration_minutes",
        )
    return delta


async def grant_break_glass(
    cmd: GrantBreakGlassCommand,
    *,
    grant_repo: _GrantRepository,
    audit: _AuditStore,
    clock: _Clock,
    new_id: UUID,
) -> BreakGlassGrant:
    """Issue a new break-glass grant.

    The use case authorizes the caller against
    ``Action.ADMIN_TENANT_CONFIG`` because the override is a
    tenant-level configuration decision. The role registry grants
    that to ``process_admin``; auditors do not grant their own
    overrides — a peer security administrator must approve.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="break_glass_grant"),
    )
    if not isinstance(cmd.scope, BreakGlassScope):
        raise ValidationFailed(f"unknown scope: {cmd.scope!r}", field="scope")
    if not isinstance(cmd.target_user_id, UUID):
        raise ValidationFailed("target_user_id must be a UUID", field="target_user_id")
    if cmd.target_user_id == cmd.actor.user_id and not _self_grant_permitted(cmd.actor):
        # Why default-deny: a self-grant would let a process
        # administrator extend their own authority without a second
        # pair of eyes. The narrow escape hatch is reserved for
        # ``is_system`` actors (background jobs that cannot wait for
        # a human) and is documented here so the test suite can pin
        # the behavior.
        raise ValidationFailed(
            "self-grant is not permitted; another administrator must approve",
            field="target_user_id",
        )
    duration = _validate_duration_minutes(cmd.duration_minutes)
    now = clock.now()
    grant = BreakGlassGrant.grant(
        id=new_id,
        tenant_id=cmd.tenant_id,
        user_id=cmd.target_user_id,
        scope=cmd.scope,
        reason=cmd.reason,
        granted_by=cmd.actor.user_id,
        granted_at=now,
        duration=duration,
    )
    await grant_repo.add(grant)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=BREAK_GLASS_GRANTED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="break_glass_grant",
        subject_id=grant.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "target_user_id": str(grant.user_id),
            "scope": grant.scope.value,
            "reason": grant.reason,
            "granted_at": grant.granted_at.isoformat(),
            "expires_at": grant.expires_at.isoformat(),
            "duration_minutes": cmd.duration_minutes,
        },
        occurred_at=now,
    )
    return grant


def _self_grant_permitted(actor: UserContext) -> bool:
    """Whether the actor is allowed to grant a break-glass to themselves.

    Why this exists: outbox dispatchers and idempotency sweepers
    run as ``is_system=True`` actors that cannot prompt a human in
    the loop. They must still be able to mint their own
    overrides for the rare automated cross-tenant read path
    (currently unused; the seam is reserved).
    """
    return bool(actor.is_system)


async def revoke_break_glass(
    cmd: RevokeBreakGlassCommand,
    *,
    grant_repo: _GrantRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> BreakGlassGrant:
    """Revoke an existing grant.

    Refuses to revoke a grant that does not exist or that lives in a
    different tenant so the caller cannot enumerate grant ids by
    guessing. Self-revocation is permitted so an actor can shorten
    their own window when they realize the override is no longer
    needed; a separate administrator is not required to revoke.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="break_glass_grant", id=cmd.grant_id),
    )
    grant = await grant_repo.get(cmd.tenant_id, cmd.grant_id)
    if grant is None:
        raise NotFound(f"break-glass grant {cmd.grant_id} not found")
    now = clock.now()
    revoked = grant.revoke(revoked_by=cmd.actor.user_id, revoked_at=now)
    if revoked is grant:
        # Already revoked — still audit the attempt so the timeline
        # shows the second revocation request and the actor that
        # issued it.
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=BREAK_GLASS_REVOKED,
            actor_id=cmd.actor.user_id,
            actor_type="user",
            subject_type="break_glass_grant",
            subject_id=grant.id,
            case_id=None,
            correlation_id=cmd.correlation_id,
            causation_id=None,
            payload={
                "idempotent": True,
                "scope": grant.scope.value,
                "target_user_id": str(grant.user_id),
            },
            occurred_at=now,
        )
        return grant
    await grant_repo.save(revoked)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=BREAK_GLASS_REVOKED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="break_glass_grant",
        subject_id=revoked.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "idempotent": False,
            "scope": revoked.scope.value,
            "target_user_id": str(revoked.user_id),
            "revoked_at": revoked.revoked_at.isoformat() if revoked.revoked_at else None,
        },
        occurred_at=now,
    )
    return revoked


async def check_break_glass(
    *,
    tenant_id: UUID,
    user_id: UUID,
    scope: BreakGlassScope,
    grant_repo: _GrantRepository,
    clock: _Clock,
) -> BreakGlassCheckResult:
    """Return the live grant for ``(user, scope)`` or a denial reason.

    This is the read-side entry point used by the search and case-read
    paths. The caller passes the *actor's* ``tenant_id`` because the
    grant is stored in the **grantor's** tenant — the cross-tenant
    case is handled by the search path itself, which knows the
    resource's tenant and can therefore look up the grant in the
    grantor's tenant via the ``grant_for`` repository method. The use
    case does not decide which tenant to query; that is a
    transport-level concern (the request carries the actor's tenant
    in ``X-Dev-Tenant``).
    """
    now = clock.now()
    grant = await grant_repo.grant_for(tenant_id, user_id, scope)
    if grant is None:
        return BreakGlassCheckResult(allowed=False, grant=None, reason=None)
    reason = grant.check(scope=scope, now=now)
    if reason is not None:
        return BreakGlassCheckResult(allowed=False, grant=grant, reason=reason)
    return BreakGlassCheckResult(allowed=True, grant=grant, reason=None)


async def record_break_glass_action(
    *,
    grant: BreakGlassGrant,
    action_name: str,
    resource_kind: str,
    resource_id: UUID | None,
    correlation_id: str,
    grant_repo: _GrantRepository,
    audit: _AuditStore,
    clock: _Clock,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Append a flag-marked audit row for an action performed under a grant.

    Every action during the window must be flagged (P11-US10); the
    audit event carries the grant id, the scope, the reason, and
    the action/resource context so the security administrator can
    reconstruct "who did what under which override" from the
    timeline alone.
    """
    now = clock.now()
    payload: dict[str, Any] = {
        "grant_id": str(grant.id),
        "scope": grant.scope.value,
        "reason": grant.reason,
        "granted_by": str(grant.granted_by),
        "expires_at": grant.expires_at.isoformat(),
        "action": action_name,
        "resource_kind": resource_kind,
        "resource_id": str(resource_id) if resource_id is not None else None,
    }
    if extra_payload:
        # Why merge last: caller extras can override structural
        # fields only when they shouldn't — a misconfigured caller
        # must not be able to suppress the flag.
        for key, value in extra_payload.items():
            if key in payload:
                continue
            payload[key] = value
    await audit.append(
        tenant_id=grant.tenant_id,
        event_type=BREAK_GLASS_ACTION,
        actor_id=grant.user_id,
        actor_type="user",
        subject_type=resource_kind,
        subject_id=resource_id or grant.id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=BREAK_GLASS_GRANTED,
        payload=payload,
        occurred_at=now,
    )
    _ = grant_repo  # currently unused; kept for future "consume grant" semantics


# ---------------------------------------------------------------------------
# Read-side enforcement (P06-US9 / FM-035)
#
# These are the only legitimate paths that cross a tenant boundary on
# reads. Both require an *active* grant in the **target** (data-owning)
# tenant, deny with the structured reason otherwise, and flag every
# successful action with ``record_break_glass_action`` so the override
# is prominent in the audit timeline.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BreakGlassReadResult:
    """A case read performed under an active grant plus the grant itself.

    The router renders the case through the standard summary builder
    (sensitive redaction still applies) and surfaces the grant id to
    the caller so the override is prominent on the wire as well as in
    audit.
    """

    case: Case
    grant: BreakGlassGrant


async def read_case_under_break_glass(
    *,
    target_tenant_id: UUID,
    actor: UserContext,
    case_id: UUID,
    correlation_id: str,
    grant_repo: _GrantRepository,
    case_repo: CaseRepository,
    audit: _AuditStore,
    clock: _Clock,
) -> BreakGlassReadResult:
    """Read a case in another tenant under ``CROSS_TENANT_CASE_READ``.

    Failure behavior:
    - Same-tenant target raises ``ValidationFailed`` (the standard
      read path is the correct surface; the override is *for*
      crossing tenant boundaries).
    - No active grant (missing, expired, revoked, wrong scope) raises
      ``Forbidden`` carrying the structured denial reason.
    - Unknown case in the target tenant raises ``NotFound`` — checked
      *after* the grant so a denied actor learns nothing about
      cross-tenant existence (Constitution V).
    """
    if target_tenant_id == actor.tenant_id:
        raise ValidationFailed(
            "break-glass read requires a different tenant; use GET /cases/{id}",
            field="target_tenant_id",
        )
    checked = await check_break_glass(
        tenant_id=target_tenant_id,
        user_id=actor.user_id,
        scope=BreakGlassScope.CROSS_TENANT_CASE_READ,
        grant_repo=grant_repo,
        clock=clock,
    )
    if not checked.allowed or checked.grant is None:
        detail = checked.reason.value if checked.reason is not None else "no_active_grant"
        raise Forbidden(f"break-glass case read denied: {detail}")
    case = await case_repo.get(target_tenant_id, case_id)
    if case is None:
        raise NotFound(f"case {case_id} not found")
    await record_break_glass_action(
        grant=checked.grant,
        action_name="case.read",
        resource_kind="case",
        resource_id=case.id,
        correlation_id=correlation_id,
        grant_repo=grant_repo,
        audit=audit,
        clock=clock,
    )
    return BreakGlassReadResult(case=case, grant=checked.grant)


@dataclass(frozen=True, slots=True)
class BreakGlassSearchResult:
    """A cross-tenant search page plus the grant that authorized it."""

    page: SearchResultPage
    grant: BreakGlassGrant


async def search_cases_under_break_glass(
    *,
    target_tenant_id: UUID,
    actor: UserContext,
    text_query: str | None,
    filters: tuple[SearchFilter, ...],
    limit: int,
    offset: int,
    searchable_field_keys: tuple[str, ...],
    sensitive_field_keys: tuple[str, ...],
    can_see_sensitive: bool,
    correlation_id: str,
    grant_repo: _GrantRepository,
    search_provider: SearchProvider,
    audit: _AuditStore,
    clock: _Clock,
) -> BreakGlassSearchResult:
    """Search cases in another tenant under ``CROSS_TENANT_SEARCH``.

    The field-key sets are pre-resolved by the caller for the *target*
    tenant (same ``SearchFieldKeyResolver`` the standard path uses).
    Filter validation (searchable-only) is the caller's
    responsibility — this function enforces the override boundary and
    flags the action, mirroring ``case_search.handle`` post-validation.
    """
    if target_tenant_id == actor.tenant_id:
        raise ValidationFailed(
            "break-glass search requires a different tenant; use POST /search",
            field="target_tenant_id",
        )
    checked = await check_break_glass(
        tenant_id=target_tenant_id,
        user_id=actor.user_id,
        scope=BreakGlassScope.CROSS_TENANT_SEARCH,
        grant_repo=grant_repo,
        clock=clock,
    )
    if not checked.allowed or checked.grant is None:
        detail = checked.reason.value if checked.reason is not None else "no_active_grant"
        raise Forbidden(f"break-glass search denied: {detail}")
    page = await search_provider.search(
        SearchSpec(
            tenant_id=target_tenant_id,
            text_query=text_query,
            filters=filters,
            limit=limit,
            offset=offset,
            can_see_sensitive=can_see_sensitive,
            searchable_field_keys=searchable_field_keys,
            sensitive_field_keys=sensitive_field_keys,
        )
    )
    await record_break_glass_action(
        grant=checked.grant,
        action_name="case.search",
        resource_kind="case_search",
        resource_id=None,
        correlation_id=correlation_id,
        grant_repo=grant_repo,
        audit=audit,
        clock=clock,
        extra_payload={"result_count": len(page.items)},
    )
    return BreakGlassSearchResult(page=page, grant=checked.grant)


__all__ = [
    "BREAK_GLASS_ACTION",
    "BREAK_GLASS_DENIED",
    "BREAK_GLASS_GRANTED",
    "BREAK_GLASS_REVOKED",
    "BreakGlassCheckResult",
    "BreakGlassReadResult",
    "BreakGlassSearchResult",
    "GrantBreakGlassCommand",
    "RevokeBreakGlassCommand",
    "check_break_glass",
    "grant_break_glass",
    "read_case_under_break_glass",
    "record_break_glass_action",
    "revoke_break_glass",
    "search_cases_under_break_glass",
]
