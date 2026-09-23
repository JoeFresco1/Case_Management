# ADR-037: Run A Redaction Boundary — Pure Redactor, Caller-Evaluated Grants

- Status: Proposed (2026-09-12, 002-leftovers Run A)
- Scope: `backend/app/application/cases/redactor.py`, `summary.py`, `view_case.py`,
  `backend/app/interfaces/http/routers/cases.py`, `search.py`, `security.py`,
  `backend/app/interfaces/http/schemas/cases.py`
- Related: FM-011 (sensitive controls), P11-US6, P05-US3, FM-029, ADR-031
  (break-glass keeps redaction), `specs/002-leftovers/architecture-runA.md §2`

## Context

FM-011 requires that unauthorized responses, logs, events, and search snippets
redact sensitive values. The failure mode to avoid is an access check that
lives in exactly one router while a second read path (search, timeline,
break-glass, export) leaks the value through a side channel (presence,
ordering, count, or null-vs-string distinction).

## Options considered

1. **Pure redactor driven by schema + pre-evaluated grant (chosen).**
   `redact_case_data(data, schema, can_see_sensitive)` replaces restricted
   values with `{"redacted": true, "reason": "sensitive"}` while preserving
   keys. The caller has already evaluated `CASE_READ` (deny-by-default) and
   the sensitive-read grant; the redactor enforces what the caller decided.
   - Pros: deterministic; unit-testable without the policy registry; the same
     contract serves case reads, search snippets, and document search; the
     redactor never logs values (callers log key lists/counts only).
   - Cons: every new read path must remember to call it — a forgotten call
     site leaks. Mitigated by review rule (every new read needs a
     redaction test) and by the SQL adapters projecting redacted snippets.

2. **Policy-calling redactor (redactor evaluates authorization itself).**
   - Pros: single call site harder to misuse.
   - Cons: couples a pure transformation to the policy registry; non-deterministic
     in unit tests; hides the authorization decision inside a helper.
     Rejected.

3. **Omit sensitive keys from the response instead of marking them.**
   - Pros: smaller payloads.
   - Cons: leaks through presence/ordering/count side channels. Rejected —
     keys are preserved with the marker so all actors see the same shape.

## Decision

Option 1, plus:

- Unknown keys (in `data` but absent from `schema`) pass through unchanged —
  the schema is the contract; write-time validation owns unknown-key rejection.
- Break-glass override lifts the read boundary, NOT the field-level grant:
  sensitive redaction still applies under override, keyed off the actor's home
  policy (ADR-031).
- Every new read path (report, bundle, export, ad-hoc query per T7) must
  evaluate the grant and run the redactor, with a redaction test asserting a
  forbidden actor sees markers and no values.

## Consequences

- Two-tenant isolation tests plus per-read redaction tests are merge blockers
  for every new query (spec SC-L02).
- Search-audit events record query shape + result count, never payload values.
- Today's coarse fallback (`is_sensitive` from `sensitivity_default`) stays
  until FM-007 adds the `case.read_sensitive` policy action; the router's
  redaction stays correct because the same flag drives both.

## Azure seam

Two placeholder seams touch this boundary (`docs/integrations/seams.md`):

- **Search (row 3 + spec open Q6)**: a future Cognitive Search adapter must
  project snippets through the same `redact_case_data` contract — the index
  must never become an authorization bypass.
- **Secrets (row 5)**: field-level encryption at rest (P05-US7, future slice 3)
  will sit behind `SecretProvider` → Azure Key Vault; redaction keys off
  sensitivity labels, encryption keys off the vault — labels stay in the
  schema, key material never enters domain, logs, or audit payloads.
