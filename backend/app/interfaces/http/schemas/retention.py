"""Pydantic schemas for the retention HTTP surface (FM-040 / P08-US3, US4).

The schema is the OpenAPI contract — never hand-edit at the router.
New fields land here first, then in the use case; the router only
translates between Pydantic and the application layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

RetentionCategoryName = Literal["case", "document", "audit"]
DispositionStatusName = Literal["pending", "approved", "rejected"]


class PublishRetentionPolicyRequest(BaseModel):
    """Body for ``POST /api/v1/admin/retention-policies``.

    The policy declares a retention period (``period_days``) for the
    chosen ``category`` (``case`` for v1). ``target_key`` scopes the
    policy within the category; case-level policies use the empty
    string (the tenant default). A non-empty ``target_key`` is
    required for ``document`` and ``audit`` categories (the domain
    refuses empty keys for those).
    """

    category: RetentionCategoryName
    target_key: str = Field(default="", max_length=128)
    period_days: int = Field(gt=0, le=36500)


class RetentionPolicyResponse(BaseModel):
    """The wire shape of a :class:`RetentionPolicy` row."""

    id: UUID
    tenant_id: UUID
    category: RetentionCategoryName
    target_key: str
    period_days: int
    version_number: int
    status: Literal["draft", "published", "retired"]
    published_at: datetime | None
    published_by: UUID | None
    created_at: datetime


class EligibleCaseEntryResponse(BaseModel):
    """A single entry on the eligible-for-disposition list."""

    case_id: UUID
    case_reference: str
    case_created_at: datetime
    policy_id: UUID
    due_at: datetime


class EligibleListResponse(BaseModel):
    """Wire shape for ``GET /api/v1/admin/disposition/eligible``."""

    items: list[EligibleCaseEntryResponse] = Field(default_factory=list)
    already_pending: list[UUID] = Field(default_factory=list)


class RouteDispositionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/disposition/eligible/{case_id}/route``."""

    reason: str | None = Field(default=None, max_length=1024)


class DispositionRequestResponse(BaseModel):
    """The wire shape of a :class:`DispositionRequest` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    policy_id: UUID
    category: RetentionCategoryName
    due_at: datetime
    status: DispositionStatusName
    surfaced_at: datetime
    surfaced_by: UUID | None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None


class DispositionDecisionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/disposition/{request_id}/approve|reject``.

    ``reason`` is optional for approval and required for rejection
    (the domain ``DispositionRequest.reject`` raises
    :class:`ValidationFailed` on a blank reason).
    """

    reason: str = Field(default="", max_length=1024)


# ---------------------------------------------------------------------------
# Archival + freeze + orphan schemas (P08-US5, US7, US8 / FM-041).
# ---------------------------------------------------------------------------

ArchivalTierName = Literal["hot", "warm"]
ArchivalStatusName = Literal["archived", "unfrozen"]
OrphanProposalStatusName = Literal["open", "surfaced", "dismissed"]


class ArchiveCaseRequest(BaseModel):
    """Body for ``POST /api/v1/admin/archival/cases/{case_id}/archive``.

    ``tier`` is the destination tier (``warm`` for v1; ``hot``
    is the default mutable tier and the aggregate refuses it).
    ``reason`` is required and audited.
    """

    tier: ArchivalTierName = "warm"
    reason: str = Field(..., min_length=1, max_length=1024)


class CaseArchivalResponse(BaseModel):
    """The wire shape of a :class:`CaseArchival` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    tier: ArchivalTierName
    archived_at: datetime
    archived_by: UUID
    reason: str
    status: ArchivalStatusName
    unfrozen_at: datetime | None = None
    unfrozen_by: UUID | None = None
    unfreeze_reason: str | None = None


class UnfreezeCaseRequest(BaseModel):
    """Body for ``POST /api/v1/admin/archival/cases/{case_id}/unfreeze``.

    ``reason`` is required and audited.
    """

    reason: str = Field(..., min_length=1, max_length=1024)


class DeclareDisposalFreezeRequest(BaseModel):
    """Body for ``POST /api/v1/admin/disposal-freezes``.

    ``starts_at`` (inclusive) and ``ends_at`` (exclusive) define
    the window during which the disposition approval use case
    refuses to approve any pending request. ``reason`` is
    required.
    """

    starts_at: datetime
    ends_at: datetime
    reason: str = Field(..., min_length=1, max_length=1024)


class DisposalFreezeResponse(BaseModel):
    """The wire shape of a :class:`DisposalFreeze` row."""

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


class CancelDisposalFreezeRequest(BaseModel):
    """Body for ``POST /api/v1/admin/disposal-freezes/{freeze_id}/cancel``.

    ``reason`` is required (the audit chain must explain why the
    freeze was lifted early; natural expiry via ``ends_at`` is
    self-documenting and does not require a reason).
    """

    reason: str = Field(..., min_length=1, max_length=1024)


class ActiveFreezeListResponse(BaseModel):
    """Wire shape for ``GET /api/v1/admin/disposal-freezes/active``."""

    items: list[DisposalFreezeResponse] = Field(default_factory=list)


class OrphanProposalEntryResponse(BaseModel):
    """A single entry on the orphan-proposal scan result."""

    proposal_id: UUID
    case_id: UUID
    case_reference: str
    detected_at: datetime
    last_activity_at: datetime
    reason: str
    status: OrphanProposalStatusName


class OrphanScanResponse(BaseModel):
    """Wire shape for ``POST /api/v1/admin/orphan-proposals/scan``.

    ``items`` are the freshly staged proposals. ``skipped_holds``
    are the case ids the scan skipped because they were under
    active legal hold (the pillar's "hold overrides retention"
    invariant is preserved).
    """

    items: list[OrphanProposalEntryResponse] = Field(default_factory=list)
    skipped_holds: list[UUID] = Field(default_factory=list)


class OrphanProposalListResponse(BaseModel):
    """Wire shape for ``GET /api/v1/admin/orphan-proposals``."""

    items: list[OrphanProposalEntryResponse] = Field(default_factory=list)


class ReviewOrphanProposalRequest(BaseModel):
    """Body for ``POST /api/v1/admin/orphan-proposals/{id}/review``.

    ``decision`` is either ``"surface"`` or ``"dismiss"``. The
    dismissal requires a non-blank ``reason``; surfacing does
    not (the rationale is implied by the action).
    """

    decision: Literal["surface", "dismiss"]
    reason: str = Field(default="", max_length=1024)


class OrphanProposalResponse(BaseModel):
    """The wire shape of a :class:`OrphanCaseProposal` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    detected_at: datetime
    last_activity_at: datetime
    reason: str
    status: OrphanProposalStatusName
    reviewed_at: datetime | None = None
    reviewed_by: UUID | None = None
    review_reason: str | None = None


# ---------------------------------------------------------------------------
# Defensible-deletion evidence report (FM-042 / P08-US6 / P12-US9)
# ---------------------------------------------------------------------------


class PolicyEvidenceEntry(BaseModel):
    """The published policy that authorised the disposition decision.

    The wire shape is a flattened snapshot of the
    :class:`app.domain.retention.RetentionPolicy` row the use case
    resolved for the most-recent approved disposition. The
    ``version_number`` + ``published_at`` pair is the minimum
    evidence a compliance officer needs to confirm "which policy
    was active when this record was disposed" (P12 §1 — reports
    identify their source timestamp; P08 §2 — defensible
    disposition is auditable end-to-end).
    """

    policy_id: UUID
    category: RetentionCategoryName
    target_key: str
    period_days: int
    version_number: int
    status: Literal["draft", "published", "retired"]
    published_at: datetime | None
    published_by: UUID | None


class DispositionEvidenceEntry(BaseModel):
    """A single disposition decision row on the chain of custody.

    ``status`` carries the decision (``approved`` / ``rejected`` /
    ``pending``); the report may include every status so the
    auditor sees the full history (a rejected request followed by
    a later approved one is the norm). ``decided_by`` + ``decided_at``
    are populated only when the request is no longer ``pending``;
    the field is the actor who recorded the decision (the
    aggregate calls this ``reviewed_by``).
    """

    request_id: UUID
    category: RetentionCategoryName
    status: DispositionStatusName
    surfaced_at: datetime
    surfaced_by: UUID | None
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    rationale: str | None = None


class AuditChainEntryResponse(BaseModel):
    """A single audit row on the chain of custody.

    Bounded projection: only the fields the chain-of-custody
    report explicitly renders are exposed (Constitution V — never
    leak SQL / stack / secrets / unredacted payloads). The
    ``summary`` is a one-line, audit-friendly rendering of the
    event type + subject so the auditor can scan the chain at a
    glance without joining externally.
    """

    event_id: str
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    occurred_at: datetime
    correlation_id: str
    summary: str


class EvidenceReportResponse(BaseModel):
    """The wire shape of ``GET /api/v1/retention/evidence/{case_id}``.

    The report declares ``kind="snapshot"`` (P12 §1 — snapshot vs.
    aggregate is explicit) and ``generated_at`` so the consumer
    knows exactly when the system of record was sampled. The
    ``disposed`` flag is the compliance signal: ``true`` iff at
    least one ``DispositionRequest`` is in ``approved`` status.
    Cases without an approved disposition are deliberately
    returned with an empty ``dispositions`` list and
    ``disposed=false`` — the absence from the platform-wide
    defensible-deletion log is the proof of "never silent
    deletion" (P08 §2).
    """

    case_id: UUID
    case_reference: str
    case_created_at: datetime
    tenant_id: UUID
    kind: Literal["snapshot"]
    generated_at: datetime
    actor_id: UUID
    disposed: bool
    policy: PolicyEvidenceEntry | None = None
    dispositions: list[DispositionEvidenceEntry] = Field(default_factory=list)
    audit_chain: list[AuditChainEntryResponse] = Field(default_factory=list)


__all__ = [
    "ActiveFreezeListResponse",
    "ArchiveCaseRequest",
    "AuditChainEntryResponse",
    "CancelDisposalFreezeRequest",
    "CaseArchivalResponse",
    "DeclareDisposalFreezeRequest",
    "DisposalFreezeResponse",
    "DispositionDecisionRequest",
    "DispositionEvidenceEntry",
    "DispositionRequestResponse",
    "EligibleCaseEntryResponse",
    "EligibleListResponse",
    "EvidenceReportResponse",
    "OrphanProposalEntryResponse",
    "OrphanProposalListResponse",
    "OrphanProposalResponse",
    "OrphanScanResponse",
    "PolicyEvidenceEntry",
    "PublishRetentionPolicyRequest",
    "RetentionPolicyResponse",
    "ReviewOrphanProposalRequest",
    "RouteDispositionRequest",
    "UnfreezeCaseRequest",
]
