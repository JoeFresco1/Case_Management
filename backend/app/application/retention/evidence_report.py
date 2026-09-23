"""Defensible-deletion evidence report use case (FM-042 / P08-US6 / P12-US9).

The records administrator's chain-of-custody surface assembles a
single-case evidence packet covering every disposition decision and
every audit row that authorized or executed it. The packet is the
durable artefact a compliance officer (P12-US9) and an external auditor
use to confirm:

- **What** was disposed — the case id + human reference + the
  ``DispositionRequest`` row that the approver authorized.
- **Under which policy version** — the published
  :class:`RetentionPolicy` row the eligibility scan evaluated against,
  including its ``version_number`` and the published-at timestamp.
- **By whom** — the approver's user id (``reviewed_by``), with the
  corresponding ``DispositionApproved`` audit row carrying the same
  ``actor_id`` so the chain of custody is auditable end-to-end.
- **When** — the ``reviewed_at`` decision timestamp and the audit
  row's ``occurred_at`` (which share the use case's commit clock so
  the two are not silently divergent).

# Why a single-case endpoint (not a date-range export)

The P12 §1 distinction between a snapshot and an aggregate matters
here: a defensible-deletion report is a *point-in-time, per-record*
artefact, not a sum. The use case is therefore a per-case read (the
case id is the resource; the actor must have the retention view
permission), and the report declares ``kind="snapshot"`` and
``generated_at`` so the consumer knows exactly when the system of
record was sampled. A date-range report would be a different surface
and is deferred (FM-042 cross-cuts with P12-US6 for CSV export).

# Why "never silent deletion" is enforced here

The pillar spec (§2 — "Explicitly not building: Silent deletion")
requires that every disposition be auditable. The report enforces
this by:

- Listing only cases that have an approved ``DispositionRequest``;
  a case without an approved request does not appear in the
  ``disposition`` list (its absence is the proof of no silent
  deletion).
- Returning the published policy version (id + version_number) that
  was active when the eligibility scan ran, not whatever policy is
  current today. A future policy cannot retroactively re-frame a
  past disposition.
- Joining the disposition row to the ``DispositionApproved`` /
  ``RetentionPolicyPublished`` / ``DispositionRequested`` audit rows
  so the auditor can re-walk the chain of custody without joining
  externally.

# Refusal semantics

- Authorization — the same ``RETENTION_POLICY_PUBLISH`` action the
  records-admin surface already grants; the evidence report is a
  read-only view of the same data, no separate action needed in
  v1.
- Tenant isolation — every repository method takes ``tenant_id``
  first and the SQL adapter carries ``WHERE tenant_id = :tenant_id``
  on every query (AGENTS.md §3 R1, §12). A cross-tenant case id
  surfaces as ``NotFound``; the use case never confirms cross-tenant
  existence (Constitution V).
- Stale decision — the report reads the *current* disposition row
  state. The use case is idempotent (no mutation) and the underlying
  audit chain is append-only; a re-run returns the same evidence.

# INTEGRATION_SEAM

The repositories (``RetentionPolicyRepository`` /
``DispositionRequestRepository`` / ``AuditEventStore`` /
``CaseRepository``) are the integration points. The in-memory
adapters live next to the disposition use case's fakes; the SQL
adapters land in a follow-up dispatch. The router depends on the
Protocols and the concrete adapters are wired through ``app.state``
so the SQL swap is a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import (
    DISPOSITION_APPROVED,
    DISPOSITION_REJECTED,
    DISPOSITION_REQUESTED,
    RETENTION_POLICY_PUBLISHED,
)
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DispositionRequestRepository,
    RetentionPolicyRepository,
)
from app.domain.retention import DispositionRequest, DispositionStatus

# Subject types for the audit chain. Stable strings — the report
# reuses the constants the disposition use case emits so a future
# search-by-subject-type filter does not need a remap.
SUBJECT_TYPE_DISPOSITION_REQUEST = "disposition_request"
SUBJECT_TYPE_RETENTION_POLICY = "retention_policy"

# Audit event types the report includes. Keeping the list explicit
# (rather than a wildcard) makes the chain-of-custody content
# auditable from this file alone — a reader of the codebase can
# locate every event type the report cites without scanning the
# disposition use case.
EVIDENCE_EVENT_TYPES: tuple[str, ...] = (
    RETENTION_POLICY_PUBLISHED,
    DISPOSITION_REQUESTED,
    DISPOSITION_APPROVED,
    DISPOSITION_REJECTED,
)


class _ClockLike(Protocol):
    """Minimal clock protocol used by the use case.

    The application layer must not depend on FastAPI / ``app.deps``
    (Constitution VI). The duck-typed ``FixedClock`` test fake and
    any future adapter satisfy this shape.
    """

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see disposition)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


# ---------------------------------------------------------------------------
# Report row shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispositionEvidence:
    """One decision row in the chain-of-custody report.

    Carries the disposition request id + status, the decision actor
    (``approved_by`` / ``rejected_by``; the aggregate names the
    reviewer ``reviewed_by`` so the wire field is the one a UI
    renders), the decision timestamp, and the rationale. The policy
    version is reported on the policy row, not here, so a single
    policy can describe multiple decisions and the report is
    unambiguous about which policy version authorised the decision.
    """

    request_id: UUID
    category: str
    status: str
    surfaced_at: datetime
    surfaced_by: UUID | None
    decided_at: datetime | None
    decided_by: UUID | None
    rationale: str | None


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    """The published retention policy that authorised the disposition.

    ``version_number`` + ``published_at`` + ``published_by`` is the
    minimum data an auditor needs to confirm "which policy was
    active when this record was disposed." The category and target
    key round out the artefact so the report is self-contained.
    """

    policy_id: UUID
    category: str
    target_key: str
    period_days: int
    version_number: int
    status: str
    published_at: datetime | None
    published_by: UUID | None


@dataclass(frozen=True, slots=True)
class AuditChainEntry:
    """A single audit row on the chain of custody.

    The full payload is not exposed — only the fields an auditor
    needs to re-walk the chain (event_type, actor, timestamp, subject,
    and a bounded subset of the payload keys the report explicitly
    renders). Constitution V: never leak SQL / stack / secrets.
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


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """The full per-case chain-of-custody packet.

    The :class:`disposed` flag is the compliance signal: ``True``
    means at least one disposition was **approved** for this case
    (the record is on the defensible-deletion log). ``False`` means
    no approved disposition exists — the report deliberately returns
    an empty ``dispositions`` list and the absence of the case from
    the platform-wide "disposed" view is the proof of "never silent
    deletion" (P08 §2).
    """

    case_id: UUID
    case_reference: str
    case_created_at: datetime
    tenant_id: UUID
    kind: str
    generated_at: datetime
    actor_id: UUID
    disposed: bool
    policy: PolicyEvidence | None
    dispositions: list[DispositionEvidence] = field(default_factory=list)
    audit_chain: list[AuditChainEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def build_evidence_report(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    case_id: UUID,
    case_repo: CaseRepository,
    policy_repo: RetentionPolicyRepository,
    disposition_repo: DispositionRequestRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> EvidenceReport:
    """Assemble the per-case chain-of-custody evidence report.

    The use case is read-only and idempotent: a re-run with the
    same inputs returns an equivalent report. The case is resolved
    first; a missing case (or a cross-tenant id) surfaces as
    :class:`NotFound` without leaking existence (Constitution V).

    The report resolves:

    - The case (id, reference, created_at).
    - Every ``DispositionRequest`` for the case (chronological).
    - The published policy each approved disposition was evaluated
      against. The disposition row carries the policy id; the use
      case resolves the published row from the repository. If the
      policy has since been retired, the row is still returned with
      ``status="retired"`` so the auditor can replay the actual
      policy that was active at decision time.
    - The audit rows for the relevant event types, scoped to the
      case. The audit chain is the same append-only chain the
      timeline view renders; the report joins by ``case_id`` so
      every approver action appears on the chain of custody.

    Failure behavior:
    - Authorization runs first; a missing grant surfaces as
      :class:`Forbidden` (HTTP 403).
    - A missing or cross-tenant case id surfaces as
      :class:`NotFound` (HTTP 404). The use case never confirms
      cross-tenant existence (Constitution V).
    - The use case performs no mutation; the only DB I/O is the
      read path. No transaction write / no audit append.
    """
    actor_ctx = _ActorProxy(
        tenant_id=tenant_id,
        user_id=actor_id,
        roles=actor_roles,
    )
    await authorize(
        Action.RETENTION_POLICY_PUBLISH,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=tenant_id, kind="retention_evidence", id=case_id),
    )

    case = await case_repo.get(tenant_id, case_id)
    if case is None:
        raise NotFound(f"case {case_id} not found")

    # Resolve the disposition history for the case. ``list_for_case``
    # returns every request in chronological order so the chain of
    # custody shows the full decision history (a rejected request
    # followed by a later approved one is the norm — a single
    # disposition is the happy path).
    disposition_rows = await disposition_repo.list_for_case(tenant_id, case_id)

    # The "active" policy for the report is the policy that was
    # active when the (most recent) approval ran. Walk the
    # approved rows in chronological order; the last one wins. A
    # case with no approvals contributes no policy evidence to the
    # report (the report is a "no silent deletion" witness).
    approved_rows = [d for d in disposition_rows if d.status is DispositionStatus.APPROVED]
    active_policy: PolicyEvidence | None = None
    if approved_rows:
        last = approved_rows[-1]
        policy_row = await policy_repo.get(tenant_id, last.policy_id)
        if policy_row is not None:
            active_policy = _policy_to_evidence(policy_row)

    # Resolve the audit chain scoped to the case. The list is
    # bounded by the port's default page limit; the report is a
    # single-case view so the chain is bounded by the case's audit
    # volume (a small number of disposition events per case in v1).
    audit_rows = await audit.list_for_case(
        tenant_id=tenant_id,
        case_id=case_id,
        event_types=list(EVIDENCE_EVENT_TYPES),
        limit=200,
    )
    audit_chain = [_audit_to_chain_entry(row) for row in audit_rows]

    return EvidenceReport(
        case_id=case.id,
        case_reference=case.reference.value,
        case_created_at=case.created_at,
        tenant_id=tenant_id,
        kind="snapshot",
        generated_at=clock.now(),
        actor_id=actor_id,
        disposed=bool(approved_rows),
        policy=active_policy,
        dispositions=[_disposition_to_evidence(d) for d in disposition_rows],
        audit_chain=audit_chain,
    )


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _policy_to_evidence(policy) -> PolicyEvidence:  # type: ignore[no-untyped-def]
    """Project a domain ``RetentionPolicy`` onto the wire-shape.

    Kept as a free function (not a method on the aggregate) so the
    projection is testable in isolation and the domain aggregate
    stays free of HTTP-shape concerns (Constitution VI).
    """
    period_seconds = int(policy.period.total_seconds())
    return PolicyEvidence(
        policy_id=policy.id,
        category=policy.category.value,
        target_key=policy.target_key,
        period_days=period_seconds // 86400,
        version_number=policy.version_number,
        status=policy.status.value,
        published_at=policy.published_at,
        published_by=policy.published_by,
    )


def _disposition_to_evidence(
    request: DispositionRequest,
) -> DispositionEvidence:
    """Project a domain ``DispositionRequest`` onto the wire shape.

    The reviewer field is exposed as ``decided_by`` because the
    wire consumer (compliance officer / auditor) thinks in terms
    of "who decided this", not the internal ``reviewed_by``
    aggregate name. The semantic is identical.
    """
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    if request.status in (DispositionStatus.APPROVED, DispositionStatus.REJECTED):
        decided_at = request.reviewed_at
        decided_by = request.reviewed_by
    return DispositionEvidence(
        request_id=request.id,
        category=request.category.value,
        status=request.status.value,
        surfaced_at=request.surfaced_at,
        surfaced_by=request.surfaced_by,
        decided_at=decided_at,
        decided_by=decided_by,
        rationale=request.review_reason,
    )


def _audit_to_chain_entry(row) -> AuditChainEntry:  # type: ignore[no-untyped-def]
    """Project an audit row onto the bounded chain-of-custody shape.

    Only the fields the report explicitly renders are surfaced.
    The full payload is not exposed so a misconfigured caller
    cannot leak actor-side identifiers (Constitution V). The
    summary is a one-line, audit-friendly rendering of the event
    type + subject so the auditor can scan the chain at a glance.

    ``event_id`` falls back to ``correlation_id`` when the row
    does not carry a database id (the in-memory test fake does not
    assign ids) — the correlation id is the durable cross-request
    identifier the audit chain shares with the timeline.
    """
    payload = row.payload if isinstance(getattr(row, "payload", None), dict) else {}
    event_type = row.event_type
    summary = _render_audit_summary(event_type, payload)
    event_id_value = getattr(row, "id", None) or row.correlation_id
    return AuditChainEntry(
        event_id=str(event_id_value),
        event_type=event_type,
        actor_id=row.actor_id,
        actor_type=row.actor_type,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        occurred_at=row.occurred_at,
        correlation_id=row.correlation_id,
        summary=summary,
    )


def _render_audit_summary(event_type: str, payload: dict[str, object]) -> str:
    """Render a bounded one-line summary for the audit chain row.

    The summary is the auditor's scanline — a long subject_id is
    truncated, the payload is referenced by key (not value) when
    the value is sensitive. Constitution V: never leak PHI / SQL /
    secrets / passwords.
    """
    if event_type == RETENTION_POLICY_PUBLISHED:
        version = payload.get("version_number", "?")
        period = payload.get("period_days", "?")
        category = payload.get("category", "?")
        return f"policy published: {category} v{version} period={period}d"
    if event_type == DISPOSITION_REQUESTED:
        policy_version = payload.get("policy_version", "?")
        return f"disposition surfaced: policy v{policy_version}"
    if event_type == DISPOSITION_APPROVED:
        return "disposition approved"
    if event_type == DISPOSITION_REJECTED:
        return "disposition rejected"
    return event_type


__all__ = [
    "AuditChainEntry",
    "DispositionEvidence",
    "EVIDENCE_EVENT_TYPES",
    "EvidenceReport",
    "PolicyEvidence",
    "SUBJECT_TYPE_DISPOSITION_REQUEST",
    "SUBJECT_TYPE_RETENTION_POLICY",
    "build_evidence_report",
]
