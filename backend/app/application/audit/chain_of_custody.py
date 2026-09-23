"""Deterministic chain-of-custody report (F-5; P09 / FM-044 + FM-045).

Combines the F-2 through F-4 records surface (legal holds, retention
policies, disposition decisions) with the audit services the slice
reads:

- the per-case audit chain (``AuditEventStore.list_for_case``),
- document authorization evidence (which reads were allowed/denied),
- the structured export envelope (actor / policy / time / audit).

The report is the offline-reviewable artefact a compliance officer
uses to confirm "what happened to this case, under which policy,
by whom, when — and that nothing was silently deleted or rewritten".

Why a new module instead of reusing ``export_audit``:

- ``export_audit`` is a paginated row dump with chain anchors. It
  does not overlay corrective annotations, does not join
  access-decision evidence, and does not verify the chain.
- ``evidence_report`` (retention) is scoped to disposition decisions.
  It does not cover document custody or access-decision evidence.
- ``evidence_bundle`` (exports) ships document bytes. Custody
  reporting must never ship bytes — only custody metadata
  (ids, hashes, actors, timestamps).

Invariants (acceptance):

- Original events never mutate. Corrections are compensating
  ``AuditCorrectionRecorded`` rows attached as read-only
  ``annotations`` on the original entry. The original payload,
  hash, and chain position are untouched.
- Exports record actor/policy/time/audit. The envelope carries
  ``actor_id``, the authorizing policy action (``audit.export``),
  ``generated_at``, and the chain-verification summary. When the
  caller wires ``dedupe`` + ``clock`` the use case also appends an
  ``AuditExported`` row so the export is visible on the chain.
- Unauthorized data is omitted without metadata leakage. The query
  is tenant-scoped and case-scoped; cross-tenant ids surface as
  ``NotFound``. Payloads are redaction-stripped through the same
  allowlist as the timeline; keys outside the allowlist become
  ``"***"`` with ``redacted=True``.
- Output is deterministic. Rows, annotations, and access-decision
  evidence sort by ``(occurred_at, id)``; the canonical dict uses
  sorted keys so two runs over the same chain produce byte-identical
  JSON for the same ``generated_at``.

# INTEGRATION_SEAM: read API + audit read
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from app.application.audit import view_timeline as view_timeline_uc
from app.application.audit.record_event import RecordAuditCommand, record_event
from app.application.authorization import Action, Resource, authorize
from app.domain.audit import (
    ACCESS_DECISION_RECORDED,
    AUDIT_CORRECTION_RECORDED,
    AUDIT_EXPORTED,
    CORRECTION_KEY_CORRECTED_SUMMARY,
    CORRECTION_KEY_ORIGINAL_EVENT_ID,
    CORRECTION_KEY_REASON,
    compute_hash,
    payload_hash,
)
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import AuditEventStore, CaseRepository

# ---------------------------------------------------------------------------
# Limits and policy evidence
# ---------------------------------------------------------------------------

DEFAULT_LIMIT: Final[int] = 500
MAX_LIMIT: Final[int] = 5000

# The authorizing policy action. The custody report leaves the system
# boundary (offline artefact), so it requires the separately-grantable
# export action — not the inline timeline read.
POLICY_ACTION: Final[str] = Action.AUDIT_EXPORT.value

# Payload keys recorded on the ``AuditExported`` row this report appends.
EXPORT_KEY_REASON: Final[str] = "reason"
EXPORT_KEY_CASE_ID: Final[str] = "case_id"
EXPORT_KEY_ROW_COUNT: Final[str] = "row_count"
EXPORT_KEY_CORRECTION_COUNT: Final[str] = "correction_count"
EXPORT_KEY_ACCESS_DECISION_COUNT: Final[str] = "access_decision_count"
EXPORT_KEY_CHAIN_OK: Final[str] = "chain_ok"

__all__ = [
    "AccessDecisionEvidence",
    "ChainOfCustodyQuery",
    "ChainOfCustodyReport",
    "ChainVerificationSummary",
    "CustodyAnnotation",
    "CustodyEntry",
    "DEFAULT_LIMIT",
    "EXPORT_KEY_ACCESS_DECISION_COUNT",
    "EXPORT_KEY_CASE_ID",
    "EXPORT_KEY_CHAIN_OK",
    "EXPORT_KEY_CORRECTION_COUNT",
    "EXPORT_KEY_REASON",
    "EXPORT_KEY_ROW_COUNT",
    "MAX_LIMIT",
    "POLICY_ACTION",
    "handle",
]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainOfCustodyQuery:
    """The use case input.

    ``reason`` lands on the ``AuditExported`` row verbatim; refusing a
    blank reason keeps the audit trail honest. ``include_access_decisions``
    toggles the access-decision evidence section (default on — the
    custody story is incomplete without "who was allowed to see what").
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    reason: str
    include_access_decisions: bool = True
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class CustodyAnnotation:
    """A read-only view of one compensating correction row.

    The annotation references the original by id and carries the reason
    plus the corrected summary. It never replaces the original payload.
    """

    correction_event_id: str
    original_event_id: str
    reason: str
    corrected_summary: str
    corrected_by: str
    corrected_at: str


@dataclass(frozen=True, slots=True)
class CustodyEntry:
    """One audit row on the custody report plus its attached annotations.

    ``payload`` is the redacted payload — sensitive values are replaced
    with ``"***"``. ``annotations`` lists every correction that targets
    this row, oldest first. A row that was never corrected carries an
    empty list.
    """

    id: str
    event_type: str
    occurred_at: str
    actor_id: str
    actor_type: str
    subject_type: str
    subject_id: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    hash: str
    prev_hash: str
    redacted: bool
    annotations: tuple[CustodyAnnotation, ...] = ()


@dataclass(frozen=True, slots=True)
class AccessDecisionEvidence:
    """One access-decision row scoped to the reported case.

    Only decisions whose subject (or payload ``resource_id``) equals the
    reported ``case_id`` are included. Decisions for other cases in the
    same tenant are omitted entirely — not redacted, not counted — so a
    case-scoped export cannot leak the existence of unrelated access
    attempts.
    """

    id: str
    occurred_at: str
    actor_id: str
    action: str
    result: str
    reason: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ChainVerificationSummary:
    """The result of replaying the case-scoped chain before rendering.

    ``ok`` is True iff every row's stored hash matches the recomputed
    chain. ``rows_checked`` counts the rows examined. A failed chain
    does not raise — the report renders the rows it read and reports
    ``ok=False`` so the auditor sees the corruption instead of a 500.
    """

    ok: bool
    rows_checked: int
    first_mismatch_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChainOfCustodyReport:
    """The deterministic custody packet for one case.

    ``policy_action`` names the authorizing action (``audit.export``);
    ``generated_at`` records when the system of record was sampled;
    ``chain`` reports the verification verdict over the rows in this
    packet. ``to_canonical_dict`` renders the stable wire shape.
    """

    tenant_id: str
    case_id: str
    actor_id: str
    policy_action: str
    reason: str
    generated_at: str
    chain: ChainVerificationSummary
    items: tuple[CustodyEntry, ...] = ()
    access_decisions: tuple[AccessDecisionEvidence, ...] = ()
    redacted_rows: int = 0

    def to_canonical_dict(self) -> dict[str, Any]:
        """Render the stable, JSON-canonical wire shape.

        Why a method: determinism is part of the contract, not a test
        convenience. Sorting here (rather than trusting construction
        order) keeps the byte output stable even if a future caller
        builds the report from an unsorted row source.
        """
        items = sorted(self.items, key=lambda e: (e.occurred_at, e.id))
        decisions = sorted(self.access_decisions, key=lambda d: (d.occurred_at, d.id))
        return {
            "access_decisions": [
                {
                    "action": d.action,
                    "actor_id": d.actor_id,
                    "correlation_id": d.correlation_id,
                    "id": d.id,
                    "occurred_at": d.occurred_at,
                    "reason": d.reason,
                    "result": d.result,
                }
                for d in decisions
            ],
            "actor_id": self.actor_id,
            "case_id": self.case_id,
            "chain": {
                "first_mismatch_id": self.chain.first_mismatch_id,
                "ok": self.chain.ok,
                "rows_checked": self.chain.rows_checked,
            },
            "generated_at": self.generated_at,
            "items": [
                {
                    "actor_id": e.actor_id,
                    "actor_type": e.actor_type,
                    "annotations": sorted(
                        (
                            {
                                "corrected_at": a.corrected_at,
                                "corrected_by": a.corrected_by,
                                "corrected_summary": a.corrected_summary,
                                "correction_event_id": a.correction_event_id,
                                "original_event_id": a.original_event_id,
                                "reason": a.reason,
                            }
                            for a in e.annotations
                        ),
                        key=lambda a: (a["corrected_at"], a["correction_event_id"]),
                    ),
                    "causation_id": e.causation_id,
                    "correlation_id": e.correlation_id,
                    "event_type": e.event_type,
                    "hash": e.hash,
                    "id": e.id,
                    "occurred_at": e.occurred_at,
                    "payload": e.payload,
                    "prev_hash": e.prev_hash,
                    "redacted": e.redacted,
                    "subject_id": e.subject_id,
                    "subject_type": e.subject_type,
                }
                for e in items
            ],
            "policy_action": self.policy_action,
            "reason": self.reason,
            "redacted_rows": self.redacted_rows,
            "tenant_id": self.tenant_id,
        }

    def to_canonical_json(self) -> str:
        """Encode ``to_canonical_dict`` with sorted keys and no whitespace."""
        return json.dumps(
            self.to_canonical_dict(), sort_keys=True, separators=(",", ":"), default=str
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _validate_query(q: ChainOfCustodyQuery) -> None:
    """Run the application-level invariants on ``q``."""
    if q.limit < 1 or q.limit > MAX_LIMIT:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_LIMIT}",
            field="limit",
        )
    if not q.reason or not q.reason.strip():
        raise ValidationFailed(
            "reason is required for a chain-of-custody export",
            field="reason",
        )


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply the timeline allowlist to one payload.

    Why reuse the timeline allowlist: the custody report must be no
    more revealing than the timeline entry it shadows. Chain anchors
    (``hash`` / ``prev_hash``) are integrity metadata and are never
    redacted — only payload content is.
    """
    safe = view_timeline_uc.PAYLOAD_SAFE_KEYS
    redacted = False
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in safe:
            out[key] = value
            continue
        redacted = True
        out[key] = "***"
    return out, redacted


def _get(row: Any, name: str, default: Any = "") -> Any:
    """Read one attribute off a duck-typed persistence row.

    Why a helper: the port stays generic so the in-memory fake, the
    SQL adapter, and a future Azure reader all satisfy it. A variable
    ``name`` keeps the B009 lint rule quiet (constant-``getattr`` is
    banned repo-wide); callers keep attribute access in one place.
    """
    return getattr(row, name, default)


def _occurred_str(row: Any) -> str:
    """Render a row's ``occurred_at`` as ISO-8601 (or ``str`` fallback)."""
    occurred = _get(row, "occurred_at", None)
    return occurred.isoformat() if isinstance(occurred, datetime) else str(occurred)


def _sort_key(row: Any) -> tuple[str, str]:
    """Deterministic order for audit rows: ``(occurred_at, id)``.

    Why stringify: the in-memory fake and the SQL adapter expose
    ``occurred_at`` as a datetime and ``id`` as a UUID; comparing the
    ISO form plus the string id keeps the order stable across both
    without importing persistence types into the application layer.
    """
    return (_occurred_str(row), str(_get(row, "id", "")))


def _row_to_annotation(row: Any) -> CustodyAnnotation:
    """Project a correction row onto the annotation overlay shape."""
    payload = dict(_get(row, "payload", None) or {})
    return CustodyAnnotation(
        correction_event_id=str(_get(row, "id", "")),
        original_event_id=str(payload.get(CORRECTION_KEY_ORIGINAL_EVENT_ID, "")),
        reason=str(payload.get(CORRECTION_KEY_REASON, "")),
        corrected_summary=str(payload.get(CORRECTION_KEY_CORRECTED_SUMMARY, "")),
        corrected_by=str(_get(row, "actor_id", "")),
        corrected_at=_occurred_str(row),
    )


def _row_to_entry(row: Any, annotations: tuple[CustodyAnnotation, ...] = ()) -> CustodyEntry:
    """Translate a persistence row into a ``CustodyEntry``."""
    redacted_payload, redacted_flag = _redact_payload(dict(_get(row, "payload", None) or {}))
    causation = _get(row, "causation_id", None)
    return CustodyEntry(
        id=str(_get(row, "id", "")),
        event_type=str(_get(row, "event_type", "")),
        occurred_at=_occurred_str(row),
        actor_id=str(_get(row, "actor_id", "")),
        actor_type=str(_get(row, "actor_type", "")),
        subject_type=str(_get(row, "subject_type", "")),
        subject_id=str(_get(row, "subject_id", "")),
        correlation_id=str(_get(row, "correlation_id", "")),
        causation_id=str(causation) if causation is not None else None,
        payload=redacted_payload,
        hash=str(_get(row, "hash", "")),
        prev_hash=str(_get(row, "prev_hash", "")),
        redacted=redacted_flag,
        annotations=tuple(
            sorted(annotations, key=lambda a: (a.corrected_at, a.correction_event_id))
        ),
    )


def _row_targets_case(row: Any, case_id: UUID) -> bool:
    """Check whether an access-decision row concerns ``case_id``.

    Why two signals: the recorder stores the resource kind/id both as
    ``subject_type`` / ``subject_id`` columns and inside the payload
    (``resource_id``). Either match scopes the row to the case; rows
    matching neither are omitted entirely (not redacted, not counted).
    """
    wanted = str(case_id)
    if str(_get(row, "subject_id", "")) == wanted:
        return True
    payload = _get(row, "payload", None) or {}
    return isinstance(payload, dict) and str(payload.get("resource_id", "")) == wanted


def _row_to_access_evidence(row: Any) -> AccessDecisionEvidence:
    """Project an access-decision row onto the evidence shape.

    The payload keys (action / result / reason) are decision metadata,
    not protected content, so they pass through verbatim. Actor roles
    are intentionally omitted — the decision row already records who
    acted and why without expanding the disclosure surface.
    """
    payload = dict(_get(row, "payload", None) or {})
    return AccessDecisionEvidence(
        id=str(_get(row, "id", "")),
        occurred_at=_occurred_str(row),
        actor_id=str(_get(row, "actor_id", "")),
        action=str(payload.get("action", "")),
        result=str(payload.get("result", "")),
        reason=str(payload.get("reason", "")),
        correlation_id=str(_get(row, "correlation_id", "")),
    )


def _verify(rows: list[Any]) -> ChainVerificationSummary:
    """Verify per-row self-hashes in deterministic order.

    Why per-row instead of ``verify_chain`` linkage: the packet is a
    case-scoped subset of a tenant-interleaved chain, so consecutive
    packet rows are rarely consecutive chain rows. Linkage across the
    subset would false-fail whenever another case's row interleaves.
    Recomputing ``compute_hash(prev_hash, payload)`` per row still
    catches payload tampering and anchor forgery; full-chain linkage
    stays the job of the tenant-wide export (P09-US3).
    """
    ordered = sorted(rows, key=_sort_key)
    checked = 0
    for row in ordered:
        checked += 1
        payload = dict(_get(row, "payload", None) or {})
        expected = compute_hash(str(_get(row, "prev_hash", "")), payload)
        if expected != str(_get(row, "hash", "")):
            return ChainVerificationSummary(
                ok=False,
                rows_checked=checked,
                first_mismatch_id=str(_get(row, "id", "")),
            )
    return ChainVerificationSummary(ok=True, rows_checked=checked)


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    query: ChainOfCustodyQuery,
    *,
    audit: AuditEventStore,
    case_repo: CaseRepository,
    clock: Any | None = None,
    dedupe: Any | None = None,
    correlation_id: str = "",
) -> ChainOfCustodyReport:
    """Build the deterministic chain-of-custody packet for one case.

    Flow:

    1. Authorize ``audit.export`` on the actor (the packet leaves the
       system boundary, so the separately-grantable export action is
       required — not the inline timeline read).
    2. Resolve the case. A missing or cross-tenant id surfaces as
       ``NotFound`` so the actor cannot probe for existence.
    3. Read the case-scoped chain (one bounded page, chronological).
       Every payload is redaction-stripped; chain anchors pass through.
    4. Overlay corrections: ``AuditCorrectionRecorded`` rows attach to
       the original entry as ``annotations``. The original row is never
       modified, reordered, or replaced.
    5. Attach access-decision evidence scoped to the case (when
       requested). Decisions for other cases are omitted entirely.
    6. Verify per-row self-hashes and stamp the envelope with
       actor/policy/time/audit. When ``dedupe`` + ``clock`` are wired,
       append the ``AuditExported`` row in the caller's transaction.

    Failure behavior:

    - Blank ``reason`` or out-of-range ``limit`` -> ``ValidationFailed``.
    - Actor missing ``audit.export`` -> ``Forbidden``.
    - Missing or cross-tenant ``case_id`` -> ``NotFound``.
    """
    _validate_query(query)

    # 1. Authorization runs before any DB touch so a denied actor
    # learns nothing about the case (Constitution V).
    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    await authorize(
        Action.AUDIT_EXPORT,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )

    # 2. Existence + tenant scoping. The repository filters by tenant;
    # a cross-tenant id resolves to None and surfaces as 404.
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        raise NotFound(f"case {query.case_id} not found")

    # 3. Read the case-scoped chain. One bounded page keeps the packet
    # deterministic; larger histories paginate via the structured
    # export instead of growing this snapshot without bound.
    rows = list(
        await audit.list_for_case(
            tenant_id=query.tenant_id,
            case_id=query.case_id,
            limit=query.limit,
        )
    )
    rows.sort(key=_sort_key)

    # 4. Split corrections from base rows. Corrections reference the
    # original by id inside their payload; the original row keeps its
    # payload, hash, and position.
    base_rows = [r for r in rows if r.event_type != AUDIT_CORRECTION_RECORDED]
    correction_rows = [r for r in rows if r.event_type == AUDIT_CORRECTION_RECORDED]
    annotations_by_original: dict[str, list[CustodyAnnotation]] = {}
    for crow in sorted(correction_rows, key=_sort_key):
        annotation = _row_to_annotation(crow)
        if not annotation.original_event_id:
            continue
        annotations_by_original.setdefault(annotation.original_event_id, []).append(annotation)

    items = tuple(
        _row_to_entry(
            row,
            tuple(annotations_by_original.get(str(_get(row, "id", "")), ())),
        )
        for row in base_rows
    )
    redacted_rows = sum(1 for entry in items if entry.redacted)

    # 5. Access-decision evidence scoped to this case. The store is
    # tenant-scoped; the case filter runs here so rows for other cases
    # are omitted entirely (no redaction, no count, no id leakage).
    access_evidence: tuple[AccessDecisionEvidence, ...] = ()
    if query.include_access_decisions:
        decision_rows = list(
            await audit.list_access_decisions(
                tenant_id=query.tenant_id,
                limit=query.limit,
            )
        )
        scoped = [r for r in decision_rows if _row_targets_case(r, query.case_id)]
        # why: drop non-case decisions (e.g. tenant-level grants) — the
        # packet must not confirm or deny access attempts on other cases.
        scoped = [r for r in scoped if str(_get(r, "event_type", "")) == ACCESS_DECISION_RECORDED]
        scoped.sort(key=_sort_key)
        access_evidence = tuple(_row_to_access_evidence(r) for r in scoped)

    # 6. Chain verification over the rows in this packet (base +
    # corrections, in chain order — the same order the store keeps).
    summary = _verify(rows)

    now = clock.now() if clock is not None and hasattr(clock, "now") else datetime.now(UTC)
    generated_at = now.isoformat() if isinstance(now, datetime) else str(now)

    # 7. Record the export itself when the caller wires idempotency.
    # The row lands in the caller's transaction so the read and the
    # audit marker are atomic. The payload carries counts plus the
    # chain verdict — never row content.
    if dedupe is not None and clock is not None:
        export_payload: dict[str, Any] = {
            EXPORT_KEY_REASON: query.reason,
            EXPORT_KEY_CASE_ID: str(query.case_id),
            EXPORT_KEY_ROW_COUNT: len(items),
            EXPORT_KEY_CORRECTION_COUNT: len(correction_rows),
            EXPORT_KEY_ACCESS_DECISION_COUNT: len(access_evidence),
            EXPORT_KEY_CHAIN_OK: summary.ok,
        }
        cid = correlation_id or str(payload_hash(export_payload))
        await record_event(
            RecordAuditCommand(
                tenant_id=query.tenant_id,
                actor_id=query.actor_id,
                actor_type="user",
                event_type=AUDIT_EXPORTED,
                subject_type="case",
                subject_id=query.case_id,
                case_id=query.case_id,
                correlation_id=cid,
                causation_id=None,
                payload=export_payload,
                occurred_at=now,
            ),
            audit=audit,
            dedupe=dedupe,
        )

    return ChainOfCustodyReport(
        tenant_id=str(query.tenant_id),
        case_id=str(query.case_id),
        actor_id=str(query.actor_id),
        policy_action=POLICY_ACTION,
        reason=query.reason,
        generated_at=generated_at,
        chain=summary,
        items=items,
        access_decisions=access_evidence,
        redacted_rows=redacted_rows,
    )
