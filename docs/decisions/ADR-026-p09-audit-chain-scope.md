# ADR-026 — P09 Audit Chain Scope (per-Tenant vs Global)

**Status:** Accepted — approved via gate `gate_edf1bd8f6e88` on `task_62d31df0447c` `T01-003` at 2026-08-31 16:02:25 (`approved`)  
**Date:** 2026-08-31  
**Accepted:** 2026-08-31 (gate `gate_edf1bd8f6e88` → `approved`)  
**Owner:** Platform team  
**Related Pillars:** P09 Audit, History & Compliance, P11 Security & Identity  
**Pillar Question:** `specs/pillars/P09-audit-history-compliance/pillar.md:8` Q1  
**Tasks/Features Affected:** FM-003 `T01-003` `task_62d31df0447c`, FM-043 `T02-043` `task_704e0aa8e4c9`, FM-044 `T04-044` `task_24afdf16e3a7`, FM-046 `T05-046` `task_2c7cef14593e`, `backend/app/domain/audit.py:26`, `backend/app/infrastructure/persistence/models.py:261` `audit_events`, `backend/app/application/audit/record_event.py:1`, `AGENTS.md:267`

## Context
Audit is append-only with chained SHA-256 `domain/audit.py:26` `hash = SHA-256(prev_hash + canonical(payload_json))` same transaction as domain change + outbox `plan.md:206`, four-layer immutability `plan.md:252`. `P09 Q1` asks: is the audit chain global, per-tenant, or per-(tenant, subject)? Current intent per pillar draft: per-tenant, with a global stitched view for cross-tenant auditors; v1 ships per-tenant only. `AGENTS.md:267` says audit `hash` chain verification is per-tenant via `tenant_id`. Cross-tenant auditors would use break-glass `T04-035` `task_33be46331ffd`.

Existing constraint: `T01-003` already implements `prev_hash` per `audit_events` row with `tenant_id` required `AGENTS.md:242`; `verify-audit` CLI `spec.md:252` replays per-tenant chain.

## Decision Required
What is the canonical hash-chain partitioning?

## Viable Options

### Option A — Per-tenant chain (current, recommended for v1)
- `audit_events.prev_hash` is `MAX(hash) WHERE tenant_id=:tenant_id`; replay `verify-audit --tenant X`; cross-tenant auditor view stitches chains but never mutates them; `T05-046` SIEM stream preserves per-tenant `prev_hash`.
- **Pros:** Aligns with single-DB `tenant_id` `ADR-021`; isolation testable `T01-002` with two tenants; no cross-tenant lock; `sqlite` replay simple.
- **Cons:** Global ordering not tamper-evident across tenants; cross-tenant timeline needs stitching sort by `occurred_at`.

### Option B — Global chain (single `prev_hash` ignoring tenant)
- `prev_hash` is global max; all tenants share one chain.
- **Pros:** Total ordering tamper-evident globally.
- **Cons:** Violates tenant isolation `AGENTS.md:67` (global lock, contention on every write, cross-tenant `hash` couples unrelated tenants); breaks `T01-002` isolation; WSL `docker` bottleneck would serialize all tenants; impossible with DB-per-tenant `ADR-021` future.

### Option C — Per-(tenant, subject_type, subject_id) chain
- Chain per case `WHERE tenant_id=:tid AND subject_id=:case_id`; `prev_hash` per case history.
- **Pros:** Fine-grained parallelism per case; natural for `T02-043` timeline per case.
- **Cons:** Per-case chain misses cross-case audit events (e.g., `ConfigurationPublished` `T03-021`); would need secondary global index to verify; `T04-044` export would need to stitch many chains; current `audit_events` DDL has `case_id` nullable, not always.

## Consequences / Tradeoffs
| Option | Schema/Migration | Public Contract | Tenant Isolation | Event Compatibility | Reversibility |
|---|---|---|---|---|---|
| A | `prev_hash` per tenant (one index `ix_audit_tenant_prev`) | `/timeline` `Page` per tenant `T02-043` | Strong (per-tenant) | Events carry `tenant_id`, SIEM batches per tenant `T05-046` | Medium — changing to B/C requires rehash of all rows (irreversible without re-seed) |
| B | Global prev (one row lock) | Global timeline would leak | Weak | Same but global hash | Hard — rehash all rows + downtime |
| C | Per-case prev (many chains) | Per-case timeline natural | Strong per-case but cross-case weak | Events need case scope | Hard — rehash per case + stitching |

## Recommended Option
**Option A — Per-tenant chain for v1.**

## Why Recommended
- Faithful to pillar intent `P09/pillar.md:8` Q1 "Current intent: per-tenant, with a global view that stitches tenant chains; v1 ships per-tenant only" — record, not reopen.
- Enables tenant isolation tests `T01-002` to verify hash without cross-tenant read; supports `T04-035` break-glass stitched view without global lock.
- Keeps `T06-MIG` portable and `T05-046` SIEM per-tenant batches `INTEGRATION_SEAM: Azure SIEM` `AGENTS.md:180`.
- `T04-044` compensating `AuditCorrectionRecorded` `plan.md:252` remains per-tenant, no cross-tenant correction.

## Reversibility / Migration Implications
- **Before any production audit rows:** Reversible by changing `compute_hash` and recomputing `prev_hash` seed 0.
- **After rows exist:** Changing scope requires recomputing every `hash` (since `prev_hash` changes) — effectively re-seeding audit; treated as **irreversible migration** requiring new chain with `supersedes` link. Gate before `T01-003` publish to avoid rehash cost.

## Tasks/Features Affected
`T01-003` `task_62d31df0447c` (hash computation, trigger, ORM listener, migration), `T02-043` `task_704e0aa8e4c9`, `T04-044` `task_24afdf16e3a7`, `T05-046` `task_2c7cef14593e`, `T06-MIG` `task_c95418f27fbd`, `T06-SEC` `task_9923f0574bc9`. FM-003, FM-043, FM-044, FM-046, FM-002.

## Approval Gate
**Created:** `gate_P09_audit_scope` on `task_62d31df0447c` `T01-003` (see report). Blocks audit foundation until scope decided.

## Existing Constraint Recorded
Existing constraint in `P09/pillar.md:8` Q1 — **per-tenant** — reaffirmed, not reopened.

## Azure Seam
SIEM stream `app/application/audit/siem_stream.py` `T05-046` `INTEGRATION_SEAM: Azure SIEM` must emit per-tenant batches with `prev_hash`; global vs per-tenant changes adapter contract.

---
*Status Accepted via gate `gate_edf1bd8f6e88` (`approved`). Do not revert without superseding ADR and rehash migration plan.*
