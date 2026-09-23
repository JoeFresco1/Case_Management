# ADR-032: Tenant-configured reference formats honored at issuance (FM-009)

- Status: Accepted (2026-09-11)
- Scope: `reference_formats` table, `SqlReferenceFormatRepository`, `next_reference` template rendering, `CaseReference` validation, `cases.reference` width.
- Supersedes: nothing (completes the "SQL adapter lands later" note in `reference_format.py`).
- Related: AGENTS.md R1/R10, FM-009 (P01-US9), FM-010 (splits inherit the type key).

## Context

The admin API stored templates in a process-local dict that
`next_reference` never read: configuration was accepted and then
silently ignored at intake. Two deeper blockers surfaced once the
wiring started: `CaseReference` hardcoded the legacy grammar (any
custom shape was unconstructable), and `cases.reference` was
`String(64)` (custom renders allow 200 chars).

## Options considered

1. **Per-template sequence counters (separate counter per shape).**
   - Pros: each template starts at 1.
   - Cons: requires recording which template issued each reference
     (schema change on `cases`) for a cosmetic benefit. Rejected.

2. **Per-tenant monotonic counter shared across shapes (chosen).**
   - Pros: matches the documented port contract
     ("monotonic per tenant"); no `cases` schema change beyond width;
     uniqueness holds across template changes by construction.
   - Cons: a template change does not restart at 1 (the legacy
     `...-000001` parses under the new shape, so the next issue is
     `...-000002`). Documented and pinned by test.

3. **Keep strict `CaseReference` grammar, restrict templates to it.**
   - Pros: no value-object change.
   - Cons: defeats FM-009 (`PR-000001` must be storable). Rejected.

## Decision

**Options 2 + relaxed value object**:

- `reference_formats` (migration `0021`, one row per tenant) +
  `SqlReferenceFormatRepository` (upsert flushes for same-transaction
  reads); the admin provider is now SQL-backed.
- `next_reference(..., type_key=None)` renders the active template;
  `{TYPE}` without a key fails closed. Sequence recovery compiles the
  template to a regex (`{SEQ}` is the capture group) and scans the
  tenant's references, plus a bounded existence-retry for the intake
  race. Default-template behavior is byte-identical to before.
- `create_case` / `split_case` / import / seed pass the type key
  (split gained an optional `case_type_repo`; the relationship bundle
  carries it).
- `CaseReference` enforces wire safety only (non-empty, ≤200 chars,
  no control characters); `cases.reference` widened to 200
  (migration `0022`). Legacy-shape strictness moved to the template
  validator, where the shape is known.

## Consequences

- 5 new SQL issuance tests + migration parity round-trip; the
  malformed-reference test now pins the wire-safety contract.
- Test fakes accept (and ignore) `type_key`; SQL is the only adapter
  that renders templates.

## Azure-seam note

No new seam. Reference issuance stays a local repository concern;
format administration is tenant configuration, not an enterprise
connector.
