# ADR-031: Durable break-glass grants and read-side enforcement (FM-035)

- Status: Accepted (2026-09-11)
- Scope: `break_glass_grants` table, `SqlBreakGlassGrantRepository`, `SecurityHttpRepositories.grant`, break-glass read/search endpoints.
- Supersedes: the router-local in-memory grant singleton (removed).
- Related: AGENTS.md R10/R12, FM-035 (P11-US10, P06-US9), FM-011, FM-031.

## Context

The first slice shipped grant mint/revoke mechanics backed by a
process-singleton dict: overrides vanished on restart (silently
dropping active windows *and* revocations), and no read path ever
consulted a grant — the FM-035 outcome ("auditor searches cross-tenant,
all actions flagged") was unreachable over HTTP.

## Options considered

1. **Keep in-memory, wire checks against the singleton.**
   - Pros: no migration.
   - Cons: restarts drop revocations (fail-open direction); multi-worker
     deployments diverge. Rejected — revocation loss is a security bug.

2. **Durable grants + dedicated enforcement endpoints (chosen).**
   - Pros: revocation survives restarts; the override surface is
     explicit (`/security/break-glass/...`) instead of a hidden flag
     on every read path; standard paths stay untouched.
   - Cons: one migration; auditors learn two endpoints.

3. **Thread grant checks into the standard case/search routers.**
   - Pros: one surface.
   - Cons: every read pays for a grant lookup; cross-tenant search
     needs target-schema resolution in the hot path; flagging logic
     spreads across routers. Rejected.

## Decision

**Option 2**:

- `break_glass_grants` (migration `0023`) keyed on the grantor's
  (data-owning) tenant; rows are never deleted, revocation writes
  `revoked_at`/`revoked_by`.
- `BreakGlassGrantRepository` port + SQL adapter with UTC
  normalization at the mapping boundary (SQLite returns naive
  datetimes; the domain clock is aware).
- `GET /security/break-glass/cases/{id}?target_tenant_id=` and
  `POST /security/break-glass/search`. Both deny-before-existence
  (403 carries the structured reason, never a cross-tenant oracle),
  reject same-tenant targets with 400, flag every success with
  `BreakGlassActionPerformed` (grant id, scope, reason, result
  count), and echo the grant id in `X-Break-Glass-Grant`.
- Sensitive redaction still applies under override, keyed off the
  actor's *home* policy: the grant overrides the read boundary, not
  the field-level grant.

## Consequences

- 9 new enforcement tests (grant→read→flag, expiry, revocation,
  same-tenant rejection, search→flag, repo scoping, migration parity).
- `tests/security/test_break_glass.py` router fixture moved to the
  SQL provider (the singleton it poked no longer exists).

## Azure-seam note

No new seam. The SQL repository is the local adapter behind the new
port; the Entra adapter changes *who the actor is*, not how the grant
is stored or checked.
