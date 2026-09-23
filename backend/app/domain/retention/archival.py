"""Archival tier, case archival, and disposal-freeze aggregates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Archival tier + freeze + orphan aggregates (P08-US5, US7, US8 / FM-041).
# ---------------------------------------------------------------------------
#
# These three aggregates share a single pillar-level concern: once a
# case is past its operational life, the platform still has to defend
# its continued existence. The pillar spec (§2) calls out three
# behaviours and this slice ships all three on top of the disposition
# pipeline:
#
# - **Archival**: a case is moved to a *cooler* storage tier (still
#   online, still readable, just cheaper). The case itself is
#   frozen — its mutable fields stop accepting writes while the
#   archive is in place. The unfreeze is a separate, audited action.
# - **Disposal freeze**: a tenant-wide (or case-level) directive
#   that blocks any disposition from executing inside a date
#   window. Used during litigation / regulatory windows (P08-US7).
#   The freeze does NOT auto-delete anything; it only blocks the
#   disposition pipeline from approving a request that is inside the
#   window. The application layer is responsible for consulting the
#   freeze before approving.
# - **Orphan proposals**: cases that have crossed their retention
#   horizon AND have not seen activity past the policy are surfaced
#   as *proposals* for review. The application layer NEVER auto-
#   deletes an orphan (Constitution IV — disposition is explicit and
#   audited). Proposals are reviewable; a reviewer can either
#   surface the case into the normal disposition pipeline or
#   dismiss the proposal with a rationale.
#
# All three are separate aggregates so the configuration (tier /
# freeze) stays immutable + small while the per-case row (archival /
# proposal) carries mutable lifecycle state. The pattern mirrors
# ``RetentionPolicy`` + ``DispositionRequest`` above.


class ArchivalTier(StrEnum):
    """A storage tier for a :class:`Case` (P08-US5 / FM-041).

    v1 ships two tiers:

    - ``HOT`` — the operational tier. The case is mutable; reads /
      writes flow at full latency. Every newly-created case lands
      here. The case aggregate's ``archived_tier`` is ``None``
      until an admin moves the case.
    - ``WARM`` — the cooler tier. The case stays online (reads are
      still allowed) but mutations are refused. The latency is
      higher and the cost is lower (P08 pillar §2).

    A future P3 slice may add ``COLD`` (offline / restore-from-
    archive only); the application layer is tier-agnostic — the
    tier string flows through as opaque metadata.
    """

    HOT = "hot"
    WARM = "warm"


@dataclass(frozen=True, slots=True)
class CaseArchival:
    """A per-case archival row (P08-US5 / FM-041).

    The aggregate carries the case id, the tier, the actor who
    moved the case, the timestamp, and the immutable reason. Once
    archived, the case is *frozen* — the application layer refuses
    any disposition approval while the row is in ``archived``
    status. The :meth:`unfreeze` transition clears the freeze; the
    archival row itself stays in the audit history so an auditor
    can replay the move.

    Attributes:
        id: Stable archival row id.
        tenant_id: Owning tenant.
        case_id: The case that was moved.
        tier: The destination tier (``WARM`` for v1).
        archived_at: Move timestamp.
        archived_by: The actor who moved the case.
        reason: Free-text rationale — required (a move without a
            reason leaves the audit chain unable to explain why).
        status: Lifecycle (``archived`` / ``unfrozen``).
        unfrozen_at: Unfreeze timestamp (``None`` while archived).
        unfrozen_by: The actor who unfroze (``None`` while
            archived).
        unfreeze_reason: The unfreeze rationale (``None`` while
            archived).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    tier: ArchivalTier
    archived_at: datetime
    archived_by: UUID
    reason: str
    status: ArchivalStatus
    unfrozen_at: datetime | None = None
    unfrozen_by: UUID | None = None
    unfreeze_reason: str | None = None

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def archive(
        *,
        tenant_id: UUID,
        case_id: UUID,
        tier: ArchivalTier,
        actor_id: UUID,
        at: datetime,
        reason: str,
    ) -> CaseArchival:
        """Construct a fresh ``archived`` row.

        Validation: ``reason`` is required and non-blank. A
        move without a rationale is refused (Constitution IV —
        every state change is auditable and the audit row carries
        the *why*).
        """
        if tier is ArchivalTier.HOT:
            raise ValidationFailed(
                "archival tier must not be HOT; HOT is the default mutable tier",
                field="tier",
            )
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when archiving a case",
                field="reason",
            )
        if not actor_id:
            raise InvariantViolation("actor_id is required to archive a case")
        return CaseArchival(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            tier=tier,
            archived_at=at,
            archived_by=actor_id,
            reason=reason.strip(),
            status=ArchivalStatus.ARCHIVED,
        )

    # ----- transitions -------------------------------------------------------

    def unfreeze(
        self,
        *,
        actor_id: UUID,
        at: datetime,
        reason: str,
    ) -> CaseArchival:
        """Return the row in ``unfrozen`` status.

        Idempotent against an already-unfrozen row. Requires a
        non-blank reason (the audit chain must explain why the
        freeze was lifted — the operation is the inverse of
        :meth:`archive` and the same evidence standard applies).
        """
        if self.status is ArchivalStatus.UNFROZEN:
            return self
        if not actor_id:
            raise InvariantViolation("actor_id is required to unfreeze a case")
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when unfreezing a case",
                field="reason",
            )
        return replace(
            self,
            status=ArchivalStatus.UNFROZEN,
            unfrozen_at=at,
            unfrozen_by=actor_id,
            unfreeze_reason=reason.strip(),
        )

    # ----- queries -----------------------------------------------------------

    @property
    def is_frozen(self) -> bool:
        """True iff the case is currently frozen (archived, not yet unfrozen)."""
        return self.status is ArchivalStatus.ARCHIVED

    def to_payload(self) -> dict[str, Any]:
        """Render the row as a JSON-safe dict for the audit payload."""
        out: dict[str, Any] = {
            "archival_id": str(self.id),
            "case_id": str(self.case_id),
            "tier": self.tier.value,
            "archived_at": self.archived_at.isoformat(),
            "archived_by": str(self.archived_by),
            "reason": self.reason,
            "status": self.status.value,
        }
        if self.unfrozen_at is not None:
            out["unfrozen_at"] = self.unfrozen_at.isoformat()
        if self.unfrozen_by is not None:
            out["unfrozen_by"] = str(self.unfrozen_by)
        if self.unfreeze_reason is not None:
            out["unfreeze_reason"] = self.unfreeze_reason
        return out


class ArchivalStatus(StrEnum):
    """Lifecycle states for a :class:`CaseArchival` row."""

    ARCHIVED = "archived"
    UNFROZEN = "unfrozen"


@dataclass(frozen=True, slots=True)
class DisposalFreeze:
    """A tenant-wide disposal freeze (P08-US7 / FM-041).

    The freeze declares a date range during which the disposition
    pipeline refuses to approve any pending request. The window is
    defined by ``starts_at`` (inclusive) and ``ends_at`` (exclusive).
    Multiple freezes may overlap in a tenant — the disposition
    approval use case refuses as long as ANY active freeze covers
    the evaluation moment.

    The aggregate carries the actor who declared the freeze and the
    reason. Once ``ends_at`` has passed, the freeze is *inactive*
    (no cancellation is required); an admin can also cancel a
    freeze early via :meth:`cancel`.

    Attributes:
        id: Stable freeze id.
        tenant_id: Owning tenant.
        starts_at: Window start (inclusive).
        ends_at: Window end (exclusive).
        reason: Free-text rationale — required.
        created_at: When the freeze was declared.
        created_by: The actor who declared the freeze.
        cancelled_at: Cancellation timestamp (``None`` while
            active).
        cancelled_by: Cancellation actor (``None`` while active).
        cancellation_reason: Cancellation rationale (``None``
            while active).
    """

    id: UUID
    tenant_id: UUID
    starts_at: datetime
    ends_at: datetime
    reason: str
    created_at: datetime
    created_by: UUID
    cancelled_at: datetime | None = None
    cancelled_by: UUID | None = None
    cancellation_reason: str | None = None

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def declare(
        *,
        tenant_id: UUID,
        starts_at: datetime,
        ends_at: datetime,
        reason: str,
        created_by: UUID,
        created_at: datetime,
    ) -> DisposalFreeze:
        """Construct a fresh active freeze.

        Validation:

        - ``ends_at`` must be strictly after ``starts_at`` (a
          zero-length window is meaningless; the use case refuses
          it so a misconfigured admin cannot accidentally freeze
          the pipeline forever).
        - ``reason`` is required and non-blank.
        """
        if ends_at <= starts_at:
            raise ValidationFailed(
                "ends_at must be strictly after starts_at",
                field="ends_at",
            )
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when declaring a disposal freeze",
                field="reason",
            )
        if not created_by:
            raise InvariantViolation("created_by is required to declare a freeze")
        return DisposalFreeze(
            id=uuid4(),
            tenant_id=tenant_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason.strip(),
            created_at=created_at,
            created_by=created_by,
        )

    # ----- transitions -------------------------------------------------------

    def cancel(
        self,
        *,
        actor_id: UUID,
        at: datetime,
        reason: str,
    ) -> DisposalFreeze:
        """Return the freeze in ``cancelled`` status.

        Idempotent against an already-cancelled row. ``reason`` is
        required (the audit chain must explain why the freeze was
        lifted early; natural expiry via ``ends_at`` is
        self-documenting and does not require a reason).
        """
        if self.cancelled_at is not None:
            return self
        if not actor_id:
            raise InvariantViolation("actor_id is required to cancel a freeze")
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when cancelling a disposal freeze",
                field="reason",
            )
        return replace(
            self,
            cancelled_at=at,
            cancelled_by=actor_id,
            cancellation_reason=reason.strip(),
        )

    # ----- queries -----------------------------------------------------------

    def is_active_at(self, *, at: datetime) -> bool:
        """Return True iff the freeze blocks disposition at ``at``.

        A freeze is active when:

        - It has not been cancelled (``cancelled_at is None``).
        - ``at`` is inside the window
          ``[starts_at, ends_at)``.

        The disposition approval use case consults this helper to
        decide whether to refuse the approval. A freeze that
        expired naturally (no cancellation) reports inactive here
        without a separate state flip.
        """
        if self.cancelled_at is not None:
            return False
        return self.starts_at <= at < self.ends_at

    def to_payload(self) -> dict[str, Any]:
        """Render the freeze as a JSON-safe dict for the audit payload."""
        out: dict[str, Any] = {
            "freeze_id": str(self.id),
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "created_by": str(self.created_by),
        }
        if self.cancelled_at is not None:
            out["cancelled_at"] = self.cancelled_at.isoformat()
        if self.cancelled_by is not None:
            out["cancelled_by"] = str(self.cancelled_by)
        if self.cancellation_reason is not None:
            out["cancellation_reason"] = self.cancellation_reason
        return out


def is_freeze_blocking(
    *,
    freezes: list[DisposalFreeze],
    at: datetime,
) -> bool:
    """Return True iff any active :class:`DisposalFreeze` blocks ``at``.

    The disposition approval use case calls this helper before
    recording the approval. If any freeze covers ``at``, the
    approval is refused with a structured ``disposal_frozen``
    error (HTTP 409 at the boundary). The function is a pure
    OR-reduction — no I/O — so the use case can call it without
    paying for an extra repository round-trip.

    Why a free function (not a method on ``DisposalFreeze``): the
    disposition use case owns the cross-aggregate policy and the
    repository returns the candidate set; keeping the reduction
    here lets the use case stay one function call away from the
    answer and keeps the aggregate free of cross-instance state.
    """
    return any(f.is_active_at(at=at) for f in freezes)
