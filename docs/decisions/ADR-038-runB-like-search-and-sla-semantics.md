# ADR-038: Portable LIKE-now search adapter and SLA clock semantics (Run B)

- Status: Accepted (2026-09-12)
- Scope: `SqlSearchProvider` (FM-031), `SlaClock` pause/resume + hold interaction + warning tier (FM-018).
- Supersedes: nothing (answers `spec.md §9` open Q6 — full-text engine choice — for the H1 horizon).
- Related: AGENTS.md R1/R10/R12, FM-031 (P06-US1), FM-018 (P02-US6), FM-039 (legal hold),
  ADR-035 (SLA authoring/visibility), `specs/002-leftovers/architecture-runB.md`.

## Context

Two decisions were deferred to the T2/T4 slice Runs (`plan.md §7`):

1. **Search engine adapter.** `spec.md §9 Q6` lists Postgres FTS vs Azure Cognitive
   Search vs OpenSearch. The domain port (`SearchProvider`) is engine-agnostic,
   but the local adapter had to be chosen before FM-031 could ship on the
   sqlite test profile and on local Postgres with identical semantics.
2. **SLA clock semantics.** `spec.md §9 Q5` asks whether a legal hold pauses or
   resets the retention/disposition clock, and the SlaClock aggregate needed a
   ruling on what `pause` means for `due_at` versus what a legal hold means for
   a counting clock.

## Options considered

### Search adapter

1. **Postgres FTS now (`tsvector`/`GIN`).**
   - Pros: real ranking, stemming, fast on large corpora.
   - Cons: `tsvector`, `GIN`, and `@@` are Postgres-only — violates AGENTS.md R1
     (portable generics, `LIKE` first); untestable on the sqlite profile, so CI
     would need Docker on a host where Docker is unavailable (AGENTS.md §3).
     Rejected for H1.
2. **Azure Cognitive Search now.**
   - Pros: managed relevance, semantic ranking built in.
   - Cons: requires a live Azure tenant on a host with no corporate network
     (AGENTS.md §3 Issues #2); couples deterministic retrieval to an external
     service before the authorization-at-result-time contract is proven.
     Rejected for H1 — remains the production-seam candidate.
3. **Portable LIKE now, FTS/Cognitive later behind the same port (chosen).**
   - Pros: identical semantics on sqlite (CI) and Postgres (local); `LIKE` on
     `title` + `"key":"value"` substring match on `data` JSON TEXT uses generic
     types only; LIKE metacharacters escaped so callers cannot widen patterns;
     the `SearchSpec`/`SearchHit` contract is unchanged when the engine swaps.
   - Cons: no stemming/ranking; substring match can false-positive when a value
     string coincides with another key's value shape; full-table scan cost on
     large tenants (acceptable at H1 volumes; pagination bounds the page).

### SLA clock semantics

1. **Hold pauses the clock (freeze `due_at`).**
   - Pros: single mechanism for "time should not count".
   - Cons: conflates two intents — an operator pause (workflowExplicit: "we are
     waiting, deadline moves") with a legal prohibition (hold: "deadline passed
     but disposition is blocked"). Freezing `due_at` on hold would hide the
     breach from the timeline, exactly when compliance needs it visible.
     Rejected.
2. **Pause freezes, hold stays visible but blocks action (chosen).**
   - Pros: each intent has one mechanism. `pause`/`resume` recompute `due_at`
     from the remaining budget (`due_at - paused_at`), so paused time never
     leaks into the deadline. A clock on a held case still flips to `breached`
     (the deadline did pass) and the audit row lands with
     `case_under_legal_hold=true`, but the escalation *action* (task creation)
     is skipped and counted in `skipped_holds` — visible, never silent.
   - Cons: operators must learn two concepts; the scanner needs the hold lookup
     seam (`_CaseLookup`) on every tick.

## Decision

**Search: Option 3. SLA: Option 2**, as implemented:

- `SqlSearchProvider` (`app/infrastructure/persistence/search/sql_search_provider.py`):
  free text matches `LOWER(title) LIKE %…%`; `data.<key>` filters match the
  `"key":"value"` JSON-TEXT substring (compact and pretty shapes); core columns
  use case-insensitive `eq`/`like`/`in`; snippet projection is bounded to
  `spec.searchable_field_keys` with per-field redaction markers.
- `SlaClock.pause` freezes (`paused_at` set, `due_at` untouched while paused);
  `resume` pushes `due_at` forward by the elapsed pause duration
  (`app/domain/sla.py`). `mark_breached`/`mark_warning` are idempotent so
  scanner retries never double audit rows. `warning_at` (`None` = tier
  disabled) drives the `active → warning → breached` path via
  `scan_warning_once` + `scan_once` in one tenant transaction
  (`app/infrastructure/persistence/sla_scanner.py`).
- `notify` escalation type remains an accepted-but-inert placeholder
  (`escalation_fired=false`, still audited) until a P04/P14 transport exists.

## Consequences

- `pytest -k "sla or Sla or SLA or search or Search or queue or approval"`
  exercises the decided semantics; two-tenant isolation tests cover every new
  query path; `test_sla_breach_visibility.py` proves breach → task → timeline
  and hold → visible-but-no-task.
- Marking a field non-searchable removes it from future filters and snippets
  (no reindex step exists for LIKE — documented behavior, not a migration).
- Retention-clock pause-vs-reset (`spec.md §9 Q5` for *retention* clocks, T6)
  is NOT decided here — this ADR covers workflow SLA clocks only; the T6 Run
  decides retention-clock semantics in its own ADR.

## Azure-seam note

No new seam. `SearchProvider` is the engine-swap seam (Cognitive Search
adapter implements the same `SearchSpec`/`SearchHit` contract; it must reuse
the sealed key sets + redaction contract, never bypass them). The scanner runs
in-process locally and as an Azure-hosted worker in production against the same
table through the same use case (`seams.md` row 1 / scheduler rows
`SLA_SCANNER_ENABLED`, `SLA_SCAN_INTERVAL_SECONDS`).
