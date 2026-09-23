"""Disposition request aggregate and the pure disposition-eligibility rule."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.retention.policy import RetentionCategory, RetentionPolicy

# ---------------------------------------------------------------------------
# Disposition request — the per-record review queue.
# ---------------------------------------------------------------------------


class DispositionStatus(StrEnum):
    """Lifecycle states for a :class:`DispositionRequest`.

    - ``PENDING`` — surfaced by the eligibility scan; awaiting an
      approver's decision.
    - ``APPROVED`` — the approver authorized disposition; the record
      will be destroyed in a separate follow-up dispatch (out of
      scope for v1 — the approval is the durable record).
    - ``REJECTED`` — the approver refused; the record stays in place
      and a fresh request will surface after the next eligibility
      scan only if the policy horizon changes (the current policy
      still applies otherwise).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DispositionRequest:
    """A per-case disposition review (P08-US4 / FM-040).

    The aggregate carries the case id, the policy id that surfaced the
    request, the category (``case`` for v1), the eligibility
    timestamp (``due_at`` — the moment the record crossed the policy
    horizon), and the lifecycle fields.

    The application use case is the only writer; the repository is
    the storage surface. Cross-tenant requests are filtered at the
    repository boundary (Constitution V — no API surface ever confirms
    cross-tenant existence).

    Attributes:
        id: Stable disposition request id.
        tenant_id: Owning tenant.
        case_id: The case the request was surfaced for.
        policy_id: The published :class:`RetentionPolicy` that
            declared the horizon.
        category: The retention category — case-level for v1.
        due_at: The moment the record crossed its retention horizon
            (``case.created_at + policy.period`` at evaluation time).
            Carried so the audit replay can recompute eligibility
            without re-running the scan.
        status: Lifecycle status.
        surfaced_at: When the request was created.
        surfaced_by: The actor who ran the scan (typically a system
            actor — see :data:`SYSTEM_BYPASS`).
        reviewed_by: The actor who approved / rejected (``None``
            while ``pending``).
        reviewed_at: The decision timestamp (``None`` while
            ``pending``).
        review_reason: The approver's rationale (``None`` while
            ``pending``). Required for a rejection (the audit chain
            never carries a meaningless rationale); recommended for
            an approval.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    policy_id: UUID
    category: RetentionCategory
    due_at: datetime
    status: DispositionStatus
    surfaced_at: datetime
    surfaced_by: UUID | None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def surface(
        *,
        tenant_id: UUID,
        case_id: UUID,
        policy_id: UUID,
        category: RetentionCategory,
        due_at: datetime,
        now: datetime,
        surfaced_by: UUID | None,
    ) -> DispositionRequest:
        """Construct a fresh pending disposition request.

        Validation:

        - ``category`` must be a real category; the aggregate refuses
          a draft-only ``RETIRED`` category marker (no such value
          exists — the call must be a valid bucket).
        - The caller (use case) is responsible for confirming the case
          is not under legal hold *before* surfacing the request;
          refusal lives at the application boundary because the hold
          aggregate is not imported here.
        """
        if category not in RetentionCategory:
            raise ValidationFailed(
                f"invalid retention category: {category!r}",
                field="category",
            )
        return DispositionRequest(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            policy_id=policy_id,
            category=category,
            due_at=due_at,
            status=DispositionStatus.PENDING,
            surfaced_at=now,
            surfaced_by=surfaced_by,
        )

    # ----- transitions -------------------------------------------------------

    def approve(
        self,
        *,
        reviewer_id: UUID,
        at: datetime,
        reason: str | None = None,
    ) -> DispositionRequest:
        """Return the request in ``approved`` status.

        Idempotent against an already-approved row (returns ``self``).
        A rejected request cannot be re-approved; the approver must
        surface a new request if the case ever re-enters the review
        queue (the use case refuses ``approve`` on a ``rejected``
        row).
        """
        if self.status is DispositionStatus.APPROVED:
            return self
        if self.status is DispositionStatus.REJECTED:
            raise InvariantViolation(f"disposition request {self.id} is rejected; cannot approve")
        if not reviewer_id:
            raise InvariantViolation("reviewer_id is required")
        return replace(
            self,
            status=DispositionStatus.APPROVED,
            reviewed_by=reviewer_id,
            reviewed_at=at,
            review_reason=(reason or "").strip() or None,
        )

    def reject(
        self,
        *,
        reviewer_id: UUID,
        at: datetime,
        reason: str,
    ) -> DispositionRequest:
        """Return the request in ``rejected`` status.

        ``reason`` is required: a rejection without a rationale
        leaves the audit chain unable to explain why the record was
        retained. Idempotent against an already-rejected row.
        """
        if self.status is DispositionStatus.REJECTED:
            return self
        if self.status is DispositionStatus.APPROVED:
            raise InvariantViolation(
                f"disposition request {self.id} is already approved; cannot reject"
            )
        if not reviewer_id:
            raise InvariantViolation("reviewer_id is required")
        if not reason or not reason.strip():
            raise ValidationFailed(
                "reason is required when rejecting a disposition request",
                field="reason",
            )
        return replace(
            self,
            status=DispositionStatus.REJECTED,
            reviewed_by=reviewer_id,
            reviewed_at=at,
            review_reason=reason.strip(),
        )

    # ----- queries -----------------------------------------------------------

    @property
    def is_pending(self) -> bool:
        """True iff the request is awaiting a decision."""
        return self.status is DispositionStatus.PENDING

    @property
    def is_decided(self) -> bool:
        """True iff the request has been approved or rejected."""
        return self.status in (
            DispositionStatus.APPROVED,
            DispositionStatus.REJECTED,
        )

    def to_payload(self) -> dict[str, Any]:
        """Render the request as a JSON-safe dict for the audit payload."""
        out: dict[str, Any] = {
            "request_id": str(self.id),
            "case_id": str(self.case_id),
            "policy_id": str(self.policy_id),
            "category": self.category.value,
            "due_at": self.due_at.isoformat(),
            "status": self.status.value,
            "surfaced_at": self.surfaced_at.isoformat(),
        }
        if self.surfaced_by is not None:
            out["surfaced_by"] = str(self.surfaced_by)
        if self.reviewed_by is not None:
            out["reviewed_by"] = str(self.reviewed_by)
        if self.reviewed_at is not None:
            out["reviewed_at"] = self.reviewed_at.isoformat()
        if self.review_reason is not None:
            out["review_reason"] = self.review_reason
        return out


# ---------------------------------------------------------------------------
# Eligibility — the pure-function rule the application layer delegates to.
# ---------------------------------------------------------------------------


def is_eligible_for_disposition(
    *,
    case_created_at: datetime,
    policy: RetentionPolicy,
    now: datetime,
    legal_hold_active: bool,
) -> bool:
    """Return True iff ``case`` is eligible for disposition at ``now``.

    The rule (P08 pillar §2):

    - The case's :class:`LegalHold` must NOT be active. Hold overrides
      retention; an active hold blocks disposition regardless of the
      horizon (Constitution V).
    - The policy must be :meth:`RetentionPolicy.is_active_at` at the
      evaluation timestamp — i.e. published and not retired.
    - The case's age (``now - case_created_at``) must be greater than
      or equal to the policy's retention period.

    Why a pure function (not a method on ``Case``): the legal-hold
    lookup is an application-layer concern (it goes through the
    repository); the case aggregate must not import the hold
    aggregate to avoid a domain cross-dependency. The application
    layer fetches the hold, then calls this function.
    """
    if legal_hold_active:
        return False
    if not policy.is_active_at(at=now):
        return False
    age = now - case_created_at
    return age >= policy.period
