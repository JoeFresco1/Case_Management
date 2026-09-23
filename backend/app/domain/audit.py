"""Audit primitives.

# INTEGRATION_SEAM: audit

Audit lives in three places:

- **Domain** (``app.domain.audit``) — `AuditEvent` value object and
  `compute_hash` helper, the canonical payload hash, the chain replay
  verifier, and the correction-event factory. Pure logic, no I/O.
  Importable from `app.application.audit`.
- **Application** (``app.application.audit.record_event``) — the use
  case that wraps `AuditEventStore.append` for use cases. Idempotent on
  ``(event_type, correlation_id, payload_hash)``.
- **Infrastructure** (``app.infrastructure.persistence.audit_outbox``) —
  the SQLAlchemy adapter that satisfies `AuditEventStore`.

Append-only is enforced at four layers (AGENTS.md §4.R12 / plan §4.R12):
DB trigger, ORM listener, no application path mutates, no migration
rewrites. Corrections are compensating events (`AuditCorrectionRecorded`)
referencing the original.

Hash chain shape (Constitution XIII.5, AGENTS.md §16.3):

    hash = SHA-256(prev_hash + 0x00 + canonical(payload_json))

``prev_hash`` for the first row of a tenant chain is the sentinel
``GENESIS_HASH`` (64 zeros). This is intentionally non-zero-looking so a
"no chain" bug is detectable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, TypedDict

# ---------------------------------------------------------------------------
# Canonical JSON + hash
# ---------------------------------------------------------------------------


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON encoding used for hashing (Constitution XIII.5).

    Why ``sort_keys=True`` and a single space separator: identical
    payloads must hash identically regardless of dict insertion order
    or whitespace.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical payload alone.

    Why a separate hash: the idempotency key in
    ``application.audit.record_event`` is computed from the business
    payload (not from the full chain anchor) so the application layer
    can deduplicate before asking the store for ``prev_hash``.
    """
    h = hashlib.sha256()
    h.update(canonical_json(payload).encode("utf-8"))
    return h.hexdigest()


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """SHA-256 over ``prev_hash + 0x00 + canonical(payload_json)``.

    Why include the previous hash: the chain is what gives tamper
    evidence; a single missing or reordered event breaks the chain.
    """
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_json(payload).encode("utf-8"))
    return h.hexdigest()


# Sentinel anchor for the first audit row of each tenant chain. Using a
# non-zero constant makes "empty chain" bugs detectable.
GENESIS_HASH: Final[str] = "0" * 64


# ---------------------------------------------------------------------------
# Event-type catalog (AGENTS.md §16.1, P09-US1..US7)
# ---------------------------------------------------------------------------


# Known event types used by the first slice. New event types must be
# added here AND in any subscriber that handles them.
CASE_CREATED: Final[str] = "CaseCreated"
CASE_UPDATED: Final[str] = "CaseUpdated"
CASE_TRANSITIONED: Final[str] = "CaseTransitioned"
# P02-US1 / FM-014 / FR-009: a workflow instance moved between states.
# Distinct from ``CaseTransitioned`` so subscribers can split
# workflow-engine state changes from case-field transitions the same
# way the catalog already separates them (see
# ``app.domain.events.WORKFLOW_TRANSITIONED``).
WORKFLOW_TRANSITIONED: Final[str] = "WorkflowTransitioned"
DOCUMENT_ADDED: Final[str] = "DocumentAdded"
DECISION_RECORDED: Final[str] = "DecisionRecorded"
TASK_CREATED: Final[str] = "TaskCreated"
TASK_COMPLETED: Final[str] = "TaskCompleted"
# P02-US6 / FM-018: the SLA scanner flipped a running clock to
# ``breached`` because ``now > due_at``. The audit row carries the
# clock's stable key, the configured breach action (id-references
# only — never free-text payloads), and the deadline; downstream
# subscribers (timeline, operator console) use the row to render
# "SLA breached at <ts> for clock <key>" without joining the
# ``sla_clocks`` table.
SLA_BREACHED: Final[str] = "SlaBreached"
# P02-US6 / FM-018 warning tier: the scanner flipped a counting
# clock to ``warning`` because ``now`` is inside the configured
# ``warning_at..due_at`` window. The audit row lets the timeline
# show "SLA warning at <ts> for clock <key>" without re-reading the
# clock row.
SLA_WARNING_RAISED: Final[str] = "SlaWarningRaised"
# P09-US5 / FM-044: a process administrator exported audit evidence.
# The export itself is a security-relevant action and lands on the
# same per-tenant chain as the events it covers.
AUDIT_EXPORTED: Final[str] = "AuditExported"
AUDIT_CORRECTION_RECORDED: Final[str] = "AuditCorrectionRecorded"
# P09-US6 / FM-045: a security-relevant authorization decision (allowed or
# denied) is appended to the chain so a security administrator can review
# who accessed which resource and why. The same row shape as every other
# audit event; ``subject_type`` / ``subject_id`` identify the resource,
# the payload carries the action, the result, and the reason.
ACCESS_DECISION_RECORDED: Final[str] = "AccessDecisionRecorded"
# P02-US3, US9 / FM-016: a worker queue was created, reconfigured,
# or retired by a process administrator. The audit row carries the
# queue's stable ``key`` so a later audit replay resolves the queue
# without scanning every aggregate.
QUEUE_DEFINED: Final[str] = "QueueDefined"
QUEUE_RECONFIGURED: Final[str] = "QueueReconfigured"
QUEUE_RETIRED: Final[str] = "QueueRetired"
# P02-US3 / FM-016: a worker explicitly claimed a queue task, then
# released it back to the queue (e.g. scope changed, picked the
# wrong item, taking a break). The two events are split so the
# timeline shows the action pair without joining.
TASK_CLAIMED: Final[str] = "TaskClaimed"
TASK_RELEASED: Final[str] = "TaskReleased"
# P02-US3 / FM-016: a worker bulk-handled a batch of queue tasks
# in a single request. The payload carries the count and the
# queue key; per-task detail is omitted by design — the timeline
# surfaces one row per task via the case's existing event stream.
TASK_BULK_HANDLED: Final[str] = "TaskBulkHandled"
# P02-US9 / FM-016: a worker delegated their open tasks to another
# identity for a bounded period (PTO handoff). The row carries the
# delegate's id and the period so the timeline can render the
# delegation window.
TASK_DELEGATED: Final[str] = "TaskDelegated"
# P02-US9 / FM-016: a time-bounded delegation expired and the task was
# restored to its prior owner (``restored_user_id``). Written by the
# ``expire_delegations`` sweep in the same transaction as the restore
# so the timeline shows the full handoff arc (delegate -> expiry ->
# restore) without joining the delegation records.
TASK_DELEGATION_EXPIRED: Final[str] = "TaskDelegationExpired"
# P08-US1, US2 / FM-039: a security administrator placed or released
# a per-case legal hold. The audit row carries the actor (placed_by /
# released_by) and the rationale so the timeline can render the hold
# lifecycle without joining the legal_holds row, and an auditor can
# replay the chain to confirm who blocked and unblocked disposition.
HOLD_PLACED: Final[str] = "HoldPlaced"
HOLD_RELEASED: Final[str] = "HoldReleased"
# P08-US3, US4 / FM-040: records-management lifecycle. A retention
# policy was published (RETENTION_POLICY_PUBLISHED), a case was
# surfaced for disposition review (DISPOSITION_REQUESTED), and an
# approver either approved (DISPOSITION_APPROVED) or rejected
# (DISPOSITION_REJECTED) the disposition. The disposition-approved
# row carries the policy version so an auditor can confirm which
# policy was active when the disposition was authorized.
#
# A held disposition that reaches execution is refused, not deleted:
# DISPOSITION_REFUSED carries code ``refused_hold`` + the policy
# version + the active hold id + the blocked scopes
# (``case``/``document``/``audit``) so the evidence report can cite
# exactly why nothing was deleted. DISPOSITION_EXECUTED marks the
# logical execution of an approved, hold-free request; physical
# destruction is a follow-up dispatch, so the row is the durable
# proof that the execution step ran without a partial delete.
RETENTION_POLICY_PUBLISHED: Final[str] = "RetentionPolicyPublished"
DISPOSITION_REQUESTED: Final[str] = "DispositionRequested"
DISPOSITION_APPROVED: Final[str] = "DispositionApproved"
DISPOSITION_REJECTED: Final[str] = "DispositionRejected"
DISPOSITION_REFUSED: Final[str] = "DispositionRefused"
DISPOSITION_EXECUTED: Final[str] = "DispositionExecuted"
# P08-US5, US7, US8 / FM-041: archival tier move + unfreeze,
# disposal-freeze declaration / cancellation, and orphan
# proposal surfacing / dismissal. Each event is the durable
# evidence for a pillar-level state change so an auditor can
# replay the lifecycle without joining the per-case rows.
CASE_ARCHIVED: Final[str] = "CaseArchived"
CASE_UNFROZEN: Final[str] = "CaseUnfrozen"
DISPOSAL_FREEZE_DECLARED: Final[str] = "DisposalFreezeDeclared"
DISPOSAL_FREEZE_CANCELLED: Final[str] = "DisposalFreezeCancelled"
ORPHAN_PROPOSAL_SURFACED: Final[str] = "OrphanProposalSurfaced"
ORPHAN_PROPOSAL_DISMISSED: Final[str] = "OrphanProposalDismissed"
# P13-US11 / FM-057: a governed GraphQL query executed against the bounded
# read surface. The payload carries the canonical query SHAPE only
# (root field, requested field names, pagination window, outcome codes)
# — never argument values, ids, or result data — so the audit row is
# safe to retain and replay under Constitution V (no PHI/secrets).
GRAPHQL_QUERY_EXECUTED: Final[str] = "GraphQLQueryExecuted"


ALL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        CASE_CREATED,
        CASE_UPDATED,
        CASE_TRANSITIONED,
        WORKFLOW_TRANSITIONED,
        DOCUMENT_ADDED,
        DECISION_RECORDED,
        TASK_CREATED,
        TASK_COMPLETED,
        SLA_BREACHED,
        SLA_WARNING_RAISED,
        AUDIT_EXPORTED,
        AUDIT_CORRECTION_RECORDED,
        ACCESS_DECISION_RECORDED,
        QUEUE_DEFINED,
        QUEUE_RECONFIGURED,
        QUEUE_RETIRED,
        TASK_CLAIMED,
        TASK_RELEASED,
        TASK_BULK_HANDLED,
        TASK_DELEGATED,
        TASK_DELEGATION_EXPIRED,
        HOLD_PLACED,
        HOLD_RELEASED,
        RETENTION_POLICY_PUBLISHED,
        DISPOSITION_REQUESTED,
        DISPOSITION_APPROVED,
        DISPOSITION_REJECTED,
        DISPOSITION_REFUSED,
        DISPOSITION_EXECUTED,
        CASE_ARCHIVED,
        CASE_UNFROZEN,
        DISPOSAL_FREEZE_DECLARED,
        DISPOSAL_FREEZE_CANCELLED,
        ORPHAN_PROPOSAL_SURFACED,
        ORPHAN_PROPOSAL_DISMISSED,
        GRAPHQL_QUERY_EXECUTED,
    }
)


# ---------------------------------------------------------------------------
# Correction payload shape (P09-US7)
# ---------------------------------------------------------------------------


# Reserved keys in an `AuditCorrectionRecorded` payload. Using named
# constants (rather than magic strings) is the AGENTS.md §4.R4
# readability requirement: a reader of the codebase can locate every
# correction payload's contract from this module.
CORRECTION_KEY_ORIGINAL_EVENT_ID: Final[str] = "original_event_id"
CORRECTION_KEY_REASON: Final[str] = "reason"
CORRECTION_KEY_CORRECTED_SUMMARY: Final[str] = "corrected_summary"


class CorrectionPayload(TypedDict, total=False):
    """The canonical shape of a compensating `AuditCorrectionRecorded` payload.

    Why TypedDict: ``app.application.audit.record_event`` validates the
    payload before appending. The TypedDict lives in domain so the
    contract is enforced where the event is defined, not in the adapter.

    The original event is **never** modified — a correction is a new
    row that references the original by id and carries the corrected
    summary plus a free-text reason (Constitution IV / P09-US7).
    """

    original_event_id: str
    reason: str
    corrected_summary: str


def make_correction_payload(
    *,
    original_event_id: str,
    reason: str,
    corrected_summary: str,
) -> dict[str, Any]:
    """Build a correction payload with the reserved keys.

    Why a factory rather than a constructor: callers sometimes construct
    corrections across trust boundaries; the factory keeps the key names
    stable so the verification command and downstream consumers can rely
    on them.
    """
    if not original_event_id:
        raise ValueError("original_event_id is required for a correction payload")
    if not reason:
        raise ValueError("reason is required for a correction payload")
    if not corrected_summary:
        raise ValueError("corrected_summary is required for a correction payload")
    return {
        CORRECTION_KEY_ORIGINAL_EVENT_ID: original_event_id,
        CORRECTION_KEY_REASON: reason,
        CORRECTION_KEY_CORRECTED_SUMMARY: corrected_summary,
    }


# ---------------------------------------------------------------------------
# Domain value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An audit event payload.

    The store stamps ``id``, ``occurred_at``, and the hash chain
    metadata; the use case supplies the business payload.
    """

    tenant_id: Any  # UUID; typed at the boundary
    event_type: str
    actor_id: Any  # UUID
    actor_type: str
    subject_type: str
    subject_id: Any  # UUID
    case_id: Any | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Chain replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    """The result of replaying the hash chain for a tenant.

    `ok` is True iff every row's stored `hash` matches
    ``compute_hash(prev_hash, payload)``. `first_mismatch_id` is the
    first row that fails verification (if any). `rows_checked` is the
    number of rows examined; useful for logging and the verify-audit
    CLI (plan §4.R9, P09-US3).
    """

    ok: bool
    rows_checked: int
    first_mismatch_id: Any | None = None


class _ChainRow(TypedDict, total=False):
    """The minimum row shape the verifier needs.

    Why a TypedDict: the verifier accepts any async iterable so it can
    work against the in-memory fake (``tests/audit/_fakes.py``), the
    SQLAlchemy adapter, or a future Azure Log Analytics reader. Each
    row must expose `id`, `hash`, `prev_hash`, and `payload`; nothing
    else is needed.
    """

    id: Any
    hash: str
    prev_hash: str
    payload: dict[str, Any]


def verify_chain(
    rows: list[_ChainRow],
    *,
    genesis_hash: str = GENESIS_HASH,
) -> ChainVerificationResult:
    """Replay ``rows`` in the order provided and verify the hash chain.

    The rows MUST be ordered chronologically (the SQL adapter orders by
    ``occurred_at, id``). The verifier is deterministic and pure; it
    does not touch the database, log, or clock.

    The chain is correct iff for every row at index ``i``:

    - ``rows[i].prev_hash == rows[i-1].hash`` (or ``genesis_hash`` for
      the first row).
    - ``rows[i].hash == compute_hash(rows[i].prev_hash, rows[i].payload)``.

    On the first failure the verifier records the row id and stops;
    subsequent rows are still counted so the operator can see how far
    the corruption extends (useful for diagnostics).

    Why a pure function: the same logic is reused by the in-memory
    test, the SQL adapter test, and the future ``verify-audit`` CLI.
    """
    rows_checked = 0
    expected_prev = genesis_hash
    for row in rows:
        rows_checked += 1
        prev_hash = row["prev_hash"]
        row_hash = row["hash"]
        payload = row["payload"]
        if prev_hash != expected_prev:
            return ChainVerificationResult(
                ok=False,
                rows_checked=rows_checked,
                first_mismatch_id=row.get("id"),
            )
        if row_hash != compute_hash(prev_hash, payload):
            return ChainVerificationResult(
                ok=False,
                rows_checked=rows_checked,
                first_mismatch_id=row.get("id"),
            )
        expected_prev = row_hash
    return ChainVerificationResult(ok=True, rows_checked=rows_checked)


__all__ = [
    "ACCESS_DECISION_RECORDED",
    "ALL_EVENT_TYPES",
    "AUDIT_CORRECTION_RECORDED",
    "AUDIT_EXPORTED",
    "AuditEvent",
    "CASE_CREATED",
    "CASE_TRANSITIONED",
    "CASE_UPDATED",
    "ChainVerificationResult",
    "CORRECTION_KEY_CORRECTED_SUMMARY",
    "CORRECTION_KEY_ORIGINAL_EVENT_ID",
    "CORRECTION_KEY_REASON",
    "DECISION_RECORDED",
    "DISPOSITION_APPROVED",
    "DISPOSITION_EXECUTED",
    "DISPOSITION_REFUSED",
    "DISPOSITION_REJECTED",
    "DISPOSITION_REQUESTED",
    "DOCUMENT_ADDED",
    "GENESIS_HASH",
    "GRAPHQL_QUERY_EXECUTED",
    "HOLD_PLACED",
    "HOLD_RELEASED",
    "RETENTION_POLICY_PUBLISHED",
    "SLA_BREACHED",
    "TASK_COMPLETED",
    "TASK_CREATED",
    "canonical_json",
    "compute_hash",
    "make_correction_payload",
    "payload_hash",
    "verify_chain",
]
