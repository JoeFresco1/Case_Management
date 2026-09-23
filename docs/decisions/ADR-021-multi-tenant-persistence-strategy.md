# ADR-021 — Multi-Tenant Persistence Strategy (Single-DB vs DB-per-Tenant)

**Status:** Proposed (awaiting approval) — do not treat as Accepted until gate resolves  
**Date:** 2026-08-31  
**Owner:** Platform team  
**Related Pillars:** P11 Security & Identity, P01 Case Business Object Management  
**Roadmap Question:** `specs/pillars/00-product-roadmap.md:131` Q1  
**Tasks/Features Affected:** FM-001 `T00-001` `task_498206edd308`, FM-002 `T01-002` `task_13d3ffcda05d`, FM-008 `T02-008` `task_6acf6097cdcb`, FM-009 `T04-009` `task_64e7c50c61cc`, all `*Repository` ports `backend/app/domain/ports.py:24`, `backend/app/infrastructure/persistence/models.py:1`, `backend/alembic/versions/*`, `AGENTS.md:130`

## Context
The platform must be multi-tenant from day one `constitution.md:471`, `.specify/memory/constitution.md V`, `AGENTS.md:67` R10. The canonical tables `AGENTS.md:242` (`tenants`, `cases`, `case_types`, etc.) all carry `tenant_id`. The deployment target is Azure SQL `AGENTS.md:29`, but v1 runs on local Postgres 16 via `docker-compose.yml` `AGENTS.md:27` and must remain testable with `sqlite+aiosqlite` `AGENTS.md:121`. No corporate DB is available `AGENTS.md:3`.

Existing constraint: Roadmap `00-product-roadmap.md:131` and `AGENTS.md:67` explicitly state **v1 = single-DB with `tenant_id` everywhere**; DB-per-tenant is a future scaling decision. The `before_compile` listener `backend/app/db.py:39` is defense-in-depth only, never sole mechanism `AGENTS.md:67`.

## Decision Required
Do we lock v1 to single-DB with explicit `tenant_id` predicates, or introduce DB-per-tenant routing now?

## Viable Options

### Option A — Single-DB with explicit `tenant_id` (current constraint)
- Every `*Repository` method takes `tenant_id` as required first-class arg `domain/ports.py:26`; every query includes `WHERE tenant_id = :tenant_id`; migrations add `tenant_id NOT NULL FK indexed` on every FK; listener `config.TENANT_LISTENER_ENABLED` is opt-in defense.
- **Pros:** Simplest v1, portable migrations, single Alembic history, sqlite test profile works, tenant isolation testable with 2 tenants in one DB, no cross-DB transactions, no connection routing.
- **Cons:** No physical noisy-neighbor isolation; row-level security relies on code correctness; scaling to 1000s tenants would need refactors.

### Option B — DB-per-tenant routing now
- `config.py:18` selects `DATABASE_URL` per `tenant_id`; `deps.py:92` routes engine/session; migrations run per DB; cross-tenant queries impossible without scatter.
- **Pros:** Strong physical isolation, per-tenant backup/restore, regulatory data residency per DB.
- **Cons:** Orders-of-magnitude complexity: connection pool per tenant, distributed migrations, no cross-tenant reporting `T04-051`, breaks `sqlite+aiosqlite` test profile, blocked by Issue #1 (no existing DB connections) `AGENTS.md:23`, irreversible without data migration.

### Option C — Single-DB now with DB-per-tenant seam
- Implement Single-DB now, but introduce `TenantRoutingProvider` port that today returns same `AsyncEngine` for all tenants; future implementation could route per tenant without changing domain. Migrations keep `tenant_id` so a future split is a data motion, not schema rewrite.
- **Pros:** Captures Option A simplicity while preserving future path; no current complexity.
- **Cons:** Slight extra indirection (port + wiring).

## Consequences / Tradeoffs
| Option | Schema/Migration Impact | Public Contract Impact | Tenant Isolation Guarantee | Irreversibility | Cost |
|---|---|---|---|---|---|
| A | Low: one DB, generic types `Uuid, JSON, DateTime` `AGENTS.md:35`, reversible | None until v2 | Logical (code) | Low — can add routing later, but splitting data is heavy | Low |
| B | High: per-DB migrations, no sqlite | None but ops contract heavy | Physical | Very high — splitting is data motion + downtime | Very high |
| C | Low + port | None | Logical today, physical later | Medium — migration still heavy but domain untouched | Low-Med |

## Recommended Option
**Option A for v1, explicitly plus Option C seam documentation** — i.e., stay Single-DB with explicit `tenant_id` and document the future seam in `docs/integrations/seams.md` and this ADR, without implementing routing now.

## Why Recommended for Current Architecture
- Satisfies explicit roadmap/architecture constraints `00-product-roadmap.md:131`, `AGENTS.md:67` without reopening.
- Preserves portable types `AGENTS.md:35` and `sqlite+aiosqlite` test harness `AGENTS.md:121`; WSL/Docker flaky `AGENTS.md:28` makes per-tenant routing untestable locally.
- Keeps blast radius small for vertical slices `P01`→`P02`→`P03` `01-deduplicated-feature-map.md:127`.
- Domain remains unaware of routing `AGENTS.md:183`; only `app/db.py`/`app/deps.py` would change later, honoring *Build locally. Architect for Azure.*

## Reversibility / Migration Implications
- **Reversible at code level:** Adding routing later is additive behind `TenantRoutingProvider`; domain unchanged.
- **Expensive at data level:** Moving from single-DB to per-tenant requires row-by-row export per `tenant_id` + outage + re-indexing; idempotency keys `idempotency_keys` table, outbox, audit hash chain `domain/audit.py:26` would need per-tenant replay. Treat as **migration, not refactor** — needs `T06-MIG` `task_c95418f27fbd` planning + downtime.
- **Gate this decision** because a wrong early adoption of B would block all H0 `T01-002`→`T02-005` and be weeks to revert.

## Tasks/Features Affected
`T00-DB` `task_186f17d88fcf`, `T01-MIG` `task_bbd86b691a63`, `T01-002` `task_13d3ffcda05d`, `T04-009` `task_64e7c50c61cc`, `T06-MIG` `task_c95418f27fbd`, `T06-INT` `task_279162523b18`. FM-001, FM-002, FM-009. No deferred scope conversion.

## Existing Constraint Recorded
Existing constraint in `AGENTS.md:67` and `00-product-roadmap.md:131` — **v1 = single-DB** — is reaffirmed, not reopened. A future `database-per-tenant` decision would require a new ADR and a funded migration slice.

## Approval Gate
Created `gate_P09` analogue is `gate_P01`? Actually this ADR maps to `T01-002`; gate on `task_13d3ffcda05d` will block authorization/foundations until approved. See gate `gate_???` created in this pass.

## Azure Seam
Relational DB seam `AGENTS.md:174` `app/db.py:39` local `asyncpg` → `infrastructure/persistence/azure_sql_*`; `TenantRoutingProvider` would be `app/infrastructure/persistence/tenant_routing.py` `INTEGRATION_SEAM: Azure SQL`.

---
*Template: Context / Decision / Options / Consequences / Recommendation / Why / Reversibility / Affected / Constraint / Gate / Seam. Status stays Proposed until gate resolves.*
