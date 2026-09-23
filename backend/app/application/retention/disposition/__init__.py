"""Retention policy + disposition use cases (P08-US3, US4 / FM-040).

The retention pillar's application surface ships four use cases:

- :func:`publish_retention_policy` — version a new retention policy
  and persist it in ``published`` status. The next
  ``version_number`` is resolved by the repository per
  ``(tenant_id, category, target_key)`` so a fresh policy in the
  same category supersedes the prior one without a global counter.
- :func:`list_eligible_for_disposition` — return the case ids that
  are past their retention horizon, are NOT under legal hold, and
  do not already have a pending disposition request. This is the
  records-admin surface ("show me what's eligible").
- :func:`approve_disposition` — record an approver's authorization
  for disposition on a pending request. The approval is durable; the
  actual case destruction is a follow-up dispatch (out of scope for
  v1).
- :func:`reject_disposition` — record an approver's refusal with a
  required rationale. The case stays in place; the rejection is
  audited so an auditor can replay who blocked the disposition and
  why.

# Refusal semantics

Three guards apply at the application boundary:

- Authorization — each use case requires a specific action
  (``RETENTION_POLICY_PUBLISH`` /
  ``DISPOSITION_APPROVE`` / ``DISPOSITION_REJECT``). Only the
  ``process_admin`` role grants these actions.
- Legal hold — :func:`list_eligible_for_disposition` filters out
  cases under active hold; the eligibility rule from
  :func:`app.domain.retention.is_eligible_for_disposition` is the
  single decision point.
- Optimistic concurrency — :func:`approve_disposition` /
  :func:`reject_disposition` look up the request by id and refuse
  if it is not in ``pending`` status, so a stale reviewer cannot
  silently overwrite a concurrent decision.

# Tenant isolation

Every repository call carries ``tenant_id`` first; the SQL adapter
must include ``WHERE tenant_id = :tenant_id`` on every query
(AGENTS.md §3 R1, §12). A cross-tenant request id surfaces as
``NotFound``; the use case never confirms cross-tenant existence
(Constitution V).

# INTEGRATION_SEAM

The repositories (``RetentionPolicyRepository`` /
``DispositionRequestRepository``) are the integration points. The
in-memory adapters live next to ``InMemoryLegalHoldRepository`` and
the SQL adapters land in a follow-up dispatch. The router depends on
the Protocol and the concrete adapter is wired through ``app.state``
so the SQL swap is a one-line change.
"""

from __future__ import annotations

from app.application.retention.disposition.commands import (
    CASE_FROZEN_CODE,
    DISPOSAL_FROZEN_CODE,
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DISPOSITION_REQUEST,
    SUBJECT_TYPE_RETENTION_POLICY,
)
from app.application.retention.disposition.decisions import (
    DispositionDecisionCommand,
    DispositionDecisionResult,
    approve_disposition,
    reject_disposition,
)
from app.application.retention.disposition.eligibility import (
    EligibleCaseEntry,
    EligibleListResult,
    list_eligible_for_disposition,
)
from app.application.retention.disposition.execute import (
    ExecuteDispositionCommand,
    ExecuteDispositionResult,
    execute_disposition,
)
from app.application.retention.disposition.publish import (
    PublishRetentionPolicyCommand,
    RetentionPolicyResult,
    publish_retention_policy,
)
from app.application.retention.disposition.route import (
    RouteDispositionCommand,
    RouteDispositionResult,
    route_disposition_for_case,
)

__all__ = [
    "CASE_FROZEN_CODE",
    "DISPOSAL_FROZEN_CODE",
    "DispositionDecisionCommand",
    "DispositionDecisionResult",
    "EligibleCaseEntry",
    "EligibleListResult",
    "ExecuteDispositionCommand",
    "ExecuteDispositionResult",
    "PublishRetentionPolicyCommand",
    "RetentionPolicyResult",
    "RouteDispositionCommand",
    "RouteDispositionResult",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_DISPOSITION_REQUEST",
    "SUBJECT_TYPE_RETENTION_POLICY",
    "approve_disposition",
    "execute_disposition",
    "list_eligible_for_disposition",
    "publish_retention_policy",
    "reject_disposition",
    "route_disposition_for_case",
]
