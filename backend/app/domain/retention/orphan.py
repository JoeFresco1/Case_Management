"""Orphan-case proposal aggregate and the v1 inactivity-window constant."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed


@dataclass(frozen=True, slots=True)
class OrphanCaseProposal:
    """A proposal that a case is an orphan (P08-US8 / FM-041).

    An *orphan* is a case that has crossed its retention horizon
    and shows no recent activity (the activity window is the
    per-tenant policy; v1 uses a fixed 90-day window so the rule is
    deterministic). The proposal is a *reviewable* row — the
    application layer never auto-deletes an orphan. A reviewer can
    either ``surface`` the case into the normal disposition
    pipeline (which creates a :class:`DispositionRequest` and
    records a ``OrphanSurfaced`` audit event) or ``dismiss`` the
    proposal with a rationale (a misclassified case, a case that
    is still in active litigation, etc.).

    Why a separate aggregate (not just a flag on the case): the
    detection rule is deterministic and the surface is
    human-readable, but the *outcome* is workflow. Splitting the
    two keeps the detection logic in the domain (pure) and the
    review state in the application layer (durable workflow row).

    Attributes:
        id: Stable proposal id.
        tenant_id: Owning tenant.
        case_id: The case the proposal is for.
        detected_at: When the proposal was created.
        last_activity_at: The case's last activity timestamp at
            detection time (for human-readable "no activity since
            <date>" hints).
        reason: Free-text explanation of why the case was flagged.
        status: Lifecycle (``open`` / ``surfaced`` /
            ``dismissed``).
        reviewed_at: Decision timestamp (``None`` while
            ``open``).
        reviewed_by: Decision actor (``None`` while ``open``).
        review_reason: Decision rationale (``None`` while
            ``open``). Required for ``dismissed``.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    detected_at: datetime
    last_activity_at: datetime
    reason: str
    status: OrphanProposalStatus
    reviewed_at: datetime | None = None
    reviewed_by: UUID | None = None
    review_reason: str | None = None

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def propose(
        *,
        tenant_id: UUID,
        case_id: UUID,
        detected_at: datetime,
        last_activity_at: datetime,
        reason: str,
    ) -> OrphanCaseProposal:
        """Construct a fresh ``open`` proposal.

        Validation: ``last_activity_at`` must be at or before
        ``detected_at`` (a future activity timestamp would be a
        clock-skew artifact; the use case refuses it so the
        reviewer does not see a confusing timeline).
        """
        if last_activity_at > detected_at:
            raise ValidationFailed(
                "last_activity_at must be at or before detected_at",
                field="last_activity_at",
            )
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required to propose a case as orphan",
                field="reason",
            )
        return OrphanCaseProposal(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            detected_at=detected_at,
            last_activity_at=last_activity_at,
            reason=reason.strip(),
            status=OrphanProposalStatus.OPEN,
        )

    # ----- transitions -------------------------------------------------------

    def surface(
        self,
        *,
        reviewer_id: UUID,
        at: datetime,
    ) -> OrphanCaseProposal:
        """Mark the proposal as surfaced into the disposition pipeline.

        Idempotent against an already-surfaced row. The actual
        disposition request is created by the application layer
        (the use case is the only writer); this method just
        captures the decision.
        """
        if self.status is OrphanProposalStatus.SURFACED:
            return self
        if self.status is OrphanProposalStatus.DISMISSED:
            raise InvariantViolation(f"orphan proposal {self.id} is dismissed; cannot surface")
        if not reviewer_id:
            raise InvariantViolation("reviewer_id is required")
        return replace(
            self,
            status=OrphanProposalStatus.SURFACED,
            reviewed_at=at,
            reviewed_by=reviewer_id,
        )

    def dismiss(
        self,
        *,
        reviewer_id: UUID,
        at: datetime,
        reason: str,
    ) -> OrphanCaseProposal:
        """Mark the proposal as dismissed with a required rationale.

        ``reason`` is required: a dismissal without a rationale
        leaves the audit chain unable to explain why the case was
        retained (Constitution IV). Idempotent against an
        already-dismissed row.
        """
        if self.status is OrphanProposalStatus.DISMISSED:
            return self
        if self.status is OrphanProposalStatus.SURFACED:
            raise InvariantViolation(f"orphan proposal {self.id} is surfaced; cannot dismiss")
        if not reviewer_id:
            raise InvariantViolation("reviewer_id is required")
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when dismissing an orphan proposal",
                field="reason",
            )
        return replace(
            self,
            status=OrphanProposalStatus.DISMISSED,
            reviewed_at=at,
            reviewed_by=reviewer_id,
            review_reason=reason.strip(),
        )

    # ----- queries -----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True iff the proposal is awaiting review."""
        return self.status is OrphanProposalStatus.OPEN

    def to_payload(self) -> dict[str, Any]:
        """Render the proposal as a JSON-safe dict for the audit payload."""
        out: dict[str, Any] = {
            "proposal_id": str(self.id),
            "case_id": str(self.case_id),
            "detected_at": self.detected_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "reason": self.reason,
            "status": self.status.value,
        }
        if self.reviewed_at is not None:
            out["reviewed_at"] = self.reviewed_at.isoformat()
        if self.reviewed_by is not None:
            out["reviewed_by"] = str(self.reviewed_by)
        if self.review_reason is not None:
            out["review_reason"] = self.review_reason
        return out


class OrphanProposalStatus(StrEnum):
    """Lifecycle states for an :class:`OrphanCaseProposal`."""

    OPEN = "open"
    SURFACED = "surfaced"
    DISMISSED = "dismissed"


# v1 orphan-detection inactivity window. The pillar spec leaves the
# exact value to a per-tenant policy (FM-041 P3). For the H1 slice
# the application layer uses this constant as the threshold: a case
# whose most recent activity is older than this is eligible for
# orphan surfacing. The value is large enough that a normal case
# lifecycle never trips it accidentally; future slices may override
# it via the published retention policy.
ORPHAN_INACTIVITY_DAYS: Final[int] = 90
