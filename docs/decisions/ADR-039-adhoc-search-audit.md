# ADR-039: Audit every ad-hoc case search (Run B follow-up)

- Status: Accepted (2026-09-12)
- Scope: `GET /cases/search`, `GET /search/cases`, `POST /search`,
  `POST /search/semantic` (FM-031 / P06-US1); `search_audit` writes (FM-032 / P06-US6).
- Supersedes: `architecture-runB.md §6` row 8 ("ad-hoc plain-search audit" gap).
- Related: AGENTS.md R12, ADR-038 (LIKE adapter), `architecture-runB.md §3`
  (search authorization trace), `app/domain/search.py:57` (audit rule).

## Context

Run B shipped search audit only on the reusable path: saved-search save +
run append a `SavedSearchAudit` row, while ad-hoc `POST /search` returned
the page with no audit write (`architecture-runB.md §6` row 8 recorded
this as an explicit gap). The task acceptance for the search track
requires every search to write audit with query shape plus count and no
PHI, so the security report can answer "who searched for what and saw
how many results" for ad-hoc executions too.

## Options considered

1. **Keep ad-hoc reads unaudited (status quo).**
   - Pros: search stays a pure read; no write amplification on the hot path.
   - Cons: fails the track acceptance; a worker can exfiltrate by searching
     without ever saving, leaving the security report blind. Rejected.
2. **Fail the search when the audit write fails (strict atomicity).**
   - Pros: "every search writes audit" holds by construction.
   - Cons: turns an observability outage into a retrieval outage for every
     worker on a read endpoint; inconsistent with read availability.
     Rejected.
3. **Append ad-hoc audit in the request transaction; rollback-and-continue
   on audit failure (chosen).**
   - Pros: every successful execution leaves one `search_audit` row with
     `saved_search_id=None`, the canonical PHI-free shape, and the result
     count; an audit outage degrades observability, not retrieval. The
     reusable saved-search path stays strict (save + run commit audit
     atomically with the domain row).
   - Cons: a narrow window where a search exists without its audit row
     (audit-table outage); acceptable for ad-hoc reads, documented here.

## Decision

Every ad-hoc case-search execution (`_run_search`, `_run_semantic_search`)
appends one `search_audit` row (`saved_search_id=None`) and commits it in
the request transaction. The query shape reuses the domain projection
(`query_shape_for_audit`): filter values targeting a sensitive key are
stored as `ref` markers, never as content. No new table or migration was
needed — `search_audit.saved_search_id` has been nullable since `0012`
exactly for this extension. `GET /search/cases` is added as a plan-path
alias of `GET /cases/search` (same use case, same gate, same audit row).

ADR-048 strengthens this projection: free text and all filter operands are
redacted in audit shapes, including when their field is not classified as
sensitive. Saved-search execution fields are retained separately for replay.

## Consequences

- `GET /search_audit` (AUDIT_READ) now covers ad-hoc and saved executions
  uniformly; saved rows are distinguishable by non-null `saved_search_id`.
- Read endpoints perform one write + commit per execution; acceptable at H1
  volumes (bounded page, single row, indexed `(tenant_id, occurred_at)`).
- Router-level tests (`test_search_cases_router.py`) pin the contract:
  two-tenant invisibility per query path, non-searchable snippet/filter
  gating, and audit shape + count with no PHI.

## Azure-seam note

No seam change. A future Cognitive Search adapter keeps the same
`SearchSpec`/`SearchHit` contract; the audit row is written by the router
from the request shape and the result count, so it is engine-agnostic.
