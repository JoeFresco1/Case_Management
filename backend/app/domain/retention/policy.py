"""Retention category enum and the versioned RetentionPolicy aggregate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

# ---------------------------------------------------------------------------
# Retention category: which bucket of records the policy applies to.
# ---------------------------------------------------------------------------


class RetentionCategory(StrEnum):
    """The buckets a retention policy applies to (P08-US3).

    v1 supports three categories — case-level, document-classification,
    audit-category. The disposition use case resolves the case's
    document classifications and audit event types to the matching
    category at evaluation time. Adding a new category requires
    updating the disposition resolver; the version bump is automatic.
    """

    CASE = "case"
    DOCUMENT = "document"
    AUDIT = "audit"


# ---------------------------------------------------------------------------
# Retention policy — the immutable, versioned configuration artefact.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """A versioned retention declaration (P08-US3 / FM-040).

    Each policy row carries a ``category``, a *target key* that scopes
    it (``"case"`` / ``"<document_classification>"`` /
    ``"<audit_event_type>"``), the retention ``period`` as a
    :class:`datetime.timedelta`, and the lifecycle metadata
    (``version_number``, ``published_at``, ``published_by``).

    The aggregate is immutable; the only mutating methods are
    :meth:`publish` and :meth:`retire`, both of which return a new
    :class:`RetentionPolicy`. The repository stores the row in the
    status it was returned in.

    Attributes:
        id: Stable policy id (UUID4).
        tenant_id: Owning tenant (FM-002 / P11-US2).
        category: The bucket the policy applies to.
        target_key: The case-classification / document-classification /
            audit-category the policy applies to. Empty string for the
            category default (``"*"`` is reserved — use ``""`` so the
            SQL adapter doesn't need a LIKE wildcard).
        period: How long the record is kept from its creation date.
        version_number: Monotonic per ``(tenant_id, category,
            target_key)`` — published via :meth:`publish`.
        status: Lifecycle status (``draft`` / ``published`` /
            ``retired``). See :class:`RetentionPolicyStatus`.
        published_at: The publish timestamp (``None`` while in
            ``draft``).
        published_by: The publish actor (``None`` while in ``draft``).
        created_at: When the draft was first staged.
    """

    id: UUID
    tenant_id: UUID
    category: RetentionCategory
    target_key: str
    period: timedelta
    version_number: int
    status: RetentionPolicyStatus
    published_at: datetime | None
    published_by: UUID | None
    created_at: datetime

    # ----- factories ---------------------------------------------------------

    @staticmethod
    def draft(
        *,
        tenant_id: UUID,
        category: RetentionCategory,
        target_key: str,
        period: timedelta,
        created_at: datetime,
    ) -> RetentionPolicy:
        """Construct a fresh draft policy.

        Validation:

        - ``period`` must be a positive ``timedelta`` (zero or negative
          retention would surface every record as eligible immediately
          — the use case refuses those as a safety net).
        - ``target_key`` is required for ``DOCUMENT`` and ``AUDIT``
          categories (case-level policies use the empty string for the
          tenant default).
        """
        if period.total_seconds() <= 0:
            raise ValidationFailed(
                "retention period must be a positive duration",
                field="period",
            )
        if category in (RetentionCategory.DOCUMENT, RetentionCategory.AUDIT) and (
            not target_key or not target_key.strip()
        ):
            raise ValidationFailed(
                f"target_key is required for {category.value} retention",
                field="target_key",
            )
        return RetentionPolicy(
            id=uuid4(),
            tenant_id=tenant_id,
            category=category,
            target_key=(target_key or "").strip(),
            period=period,
            version_number=1,  # bumped by the repository on supersede
            status=RetentionPolicyStatus.DRAFT,
            published_at=None,
            published_by=None,
            created_at=created_at,
        )

    # ----- transitions -------------------------------------------------------

    def publish(self, *, actor_id: UUID, at: datetime) -> RetentionPolicy:
        """Return the policy in ``published`` status.

        Idempotent: re-publishing a published policy returns ``self``
        unchanged (the policy is immutable once published — no
        retroactive edit). Calling :meth:`publish` on a retired
        policy raises :class:`InvariantViolation`; retirement is a
        one-way door.
        """
        if self.status is RetentionPolicyStatus.PUBLISHED:
            return self
        if self.status is RetentionPolicyStatus.RETIRED:
            raise InvariantViolation(f"retention policy {self.id} is retired; cannot republish")
        if not actor_id:
            raise InvariantViolation("actor_id is required to publish a policy")
        return replace(
            self,
            status=RetentionPolicyStatus.PUBLISHED,
            published_at=at,
            published_by=actor_id,
        )

    def retire(self) -> RetentionPolicy:
        """Mark the policy ``retired``.

        Idempotent against an already-retired row. A retired policy
        stays in the audit history (its data is preserved) but no
        longer contributes to :meth:`is_active_at` — the disposition
        use case ignores retired rows.
        """
        if self.status is RetentionPolicyStatus.RETIRED:
            return self
        if self.status is RetentionPolicyStatus.DRAFT:
            raise InvariantViolation("cannot retire an unpublished draft; discard it instead")
        return replace(self, status=RetentionPolicyStatus.RETIRED)

    # ----- queries -----------------------------------------------------------

    def is_active_at(self, *, at: datetime) -> bool:
        """Return True iff the policy is the active one at ``at``.

        The disposition use case picks the most recently published,
        non-retired policy whose ``published_at <= at`` to evaluate
        eligibility against. A draft row is never active. A retired
        row is never active. This helper is the single decision point
        — the application layer may filter and sort but the
        aggregate owns the rule.
        """
        if self.status is not RetentionPolicyStatus.PUBLISHED:
            return False
        if self.published_at is None:
            return False
        return self.published_at <= at


class RetentionPolicyStatus(StrEnum):
    """Lifecycle states for a :class:`RetentionPolicy`."""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"
