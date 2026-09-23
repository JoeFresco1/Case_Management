"""Break-glass aggregate (P11-US10 / FM-035).

# INTEGRATION_SEAM: break-glass

A break-glass grant is a time-bounded, reason-bearing override that
allows an authorized actor to perform actions they would otherwise be
denied — most notably cross-tenant auditor search (P06-US9). The
domain shape is intentionally narrow: a single immutable grant with
an expiry, a revocation surface, and a query for "is this grant
active right now for this actor on this action?".

The application layer (see ``app.application.security.break_glass``)
owns the use case that grants / revokes / checks the override; this
module owns the rules and the immutability guarantees:

- A grant cannot be extended once issued; revoking is the only way
  to shorten its lifetime.
- A grant is **active** only between ``granted_at`` and ``expires_at``
  and only when not revoked.
- Every check returns the **reason** (no live grant, expired, revoked,
  unknown scope) so the audit row carries the same explanation the
  actor would have seen.

# Why no I/O lives here

The domain is pure. The grant aggregate is constructed from validated
inputs (``BreakGlassGrant.grant``) and queried through ``is_active``
/ ``allows`` methods. The use case wires it to the audit store and
the in-process grant repository; this module imports nothing from
``app.application`` or ``app.infrastructure``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

# Maximum duration allowed for any single break-glass grant. The
# application use case rejects longer windows so a misconfigured
# caller cannot turn an emergency override into a permanent one
# (P11-US10). 24h matches the privileged-access-workstation policy
# window for the same reason.
MAX_GRANT_DURATION: Final[timedelta] = timedelta(hours=24)

# Minimum duration. A zero-second grant would be useless and almost
# certainly a client-side bug; refuse to mint one so the audit row
# still shows a meaningful "expires at" timestamp.
MIN_GRANT_DURATION: Final[timedelta] = timedelta(minutes=1)


class BreakGlassScope(StrEnum):
    """The narrow set of scopes a break-glass grant may authorize.

    Keeping the vocabulary closed ensures the audit row, the policy
    decision, and the wire shape all agree on what the override
    allows. New scopes must be added here AND in the application
    layer's allow-list.
    """

    # Cross-tenant case search (P06-US9 / FM-035). The grant
    # authorizes the actor to bypass the standard tenant filter on
    # case searches for the duration of the window. Every result the
    # actor views during the window is still audit-flagged.
    CROSS_TENANT_SEARCH = "cross_tenant_search"

    # Case read override. Reserved for an auditor scenario where the
    # actor needs to read a case in a tenant they do not belong to.
    # Every case read during the window is audit-flagged.
    CROSS_TENANT_CASE_READ = "cross_tenant_case_read"


class BreakGlassDenialReason(StrEnum):
    """Structured reason for a denied break-glass action.

    Mirrors the ``SessionDenialReason`` shape so the HTTP layer can
    map reasons to status codes uniformly. The reason is also the
    audit payload so security administrators reading the timeline
    see exactly why the override did (or did not) authorize the
    action.
    """

    NO_ACTIVE_GRANT = "no_active_grant"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SCOPE_NOT_GRANTED = "scope_not_granted"


@dataclass(frozen=True, slots=True)
class BreakGlassGrant:
    """An immutable, time-bounded override (P11-US10 / FM-035).

    The grant is the durable record of "an authorized actor
    approved this override for this user on this scope between
    these timestamps with this reason". Once issued the grant is
    read-only; revoking produces a new ``revoked_at`` and
    ``revoked_by`` without mutating the existing fields.

    Tenant scoping: the grant belongs to the *grantor's* tenant
    (the security administrator who approved the override). The
    target actor (``user_id``) may operate in a different tenant —
    that is the entire point of the cross-tenant scope — but the
    audit row always lands in the grantor's tenant so a security
    reviewer can reconstruct the decision without cross-tenant
    joins.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    scope: BreakGlassScope
    reason: str
    granted_by: UUID
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None

    @classmethod
    def grant(
        cls,
        *,
        id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        scope: BreakGlassScope,
        reason: str,
        granted_by: UUID,
        granted_at: datetime,
        duration: timedelta,
    ) -> BreakGlassGrant:
        """Mint a new, un-revoked grant.

        Validates the duration window and the reason so a misconfigured
        caller cannot produce a permanent override or a grant with an
        empty justification. Raises ``ValueError`` on invariant
        violations — the application layer maps these to
        ``ValidationFailed`` (HTTP 422).
        """
        if not isinstance(scope, BreakGlassScope):
            raise TypeError(f"unknown break-glass scope: {scope!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required and must be a non-empty string")
        if len(reason.strip()) > 1024:
            raise ValueError("reason must be 1024 characters or fewer")
        if not isinstance(duration, timedelta) or duration < MIN_GRANT_DURATION:
            min_seconds = int(MIN_GRANT_DURATION.total_seconds())
            raise ValueError(f"duration must be at least {min_seconds}s")
        if duration > MAX_GRANT_DURATION:
            max_seconds = int(MAX_GRANT_DURATION.total_seconds())
            raise ValueError(f"duration must be at most {max_seconds}s")
        return cls(
            id=id,
            tenant_id=tenant_id,
            user_id=user_id,
            scope=scope,
            reason=reason.strip(),
            granted_by=granted_by,
            granted_at=granted_at,
            expires_at=granted_at + duration,
        )

    def revoke(self, *, revoked_by: UUID, revoked_at: datetime) -> BreakGlassGrant:
        """Return a copy with the revocation fields set.

        Revoking an already-revoked grant is a no-op that returns the
        same instance; revoking an expired grant is permitted so an
        operator can record the post-hoc decision but the grant was
        already inactive.
        """
        if not isinstance(revoked_by, UUID):
            raise TypeError("revoked_by must be a UUID")
        if self.revoked_at is not None:
            return self
        return replace(self, revoked_at=revoked_at, revoked_by=revoked_by)

    def is_active(self, *, now: datetime) -> bool:
        """Return ``True`` iff the grant is live at ``now``.

        Revoked grants are never active regardless of the timestamp;
        expired grants are not active after ``expires_at``.
        """
        if self.revoked_at is not None:
            return False
        if now < self.granted_at:
            return False
        if now >= self.expires_at:  # noqa: SIM103 — explicit boundary reads more clearly than the inverted form.
            return False
        return True

    def check(self, *, scope: BreakGlassScope, now: datetime) -> BreakGlassDenialReason | None:
        """Return ``None`` if the grant covers ``scope`` at ``now``.

        Returns a :class:`BreakGlassDenialReason` enumerator when the
        grant does not authorize the requested scope so the caller can
        emit a structured audit row and HTTP 403 with the same reason.
        Order of checks matters: a revoked grant is reported as
        ``REVOKED`` even after expiry so the timeline reads correctly.
        """
        if self.revoked_at is not None:
            return BreakGlassDenialReason.REVOKED
        if now < self.granted_at:
            return BreakGlassDenialReason.EXPIRED
        if now >= self.expires_at:
            return BreakGlassDenialReason.EXPIRED
        if self.scope is not scope:
            return BreakGlassDenialReason.SCOPE_NOT_GRANTED
        return None

    def remaining(self, *, now: datetime) -> timedelta:
        """Return the time left before expiry (zero when not active)."""
        if not self.is_active(now=now):
            return timedelta(0)
        return self.expires_at - now


__all__ = [
    "BreakGlassDenialReason",
    "BreakGlassGrant",
    "BreakGlassScope",
    "MAX_GRANT_DURATION",
    "MIN_GRANT_DURATION",
]
