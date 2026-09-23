# ADR-022 — Public vs Internal API Surface

**Status:** Proposed (awaiting approval)  
**Date:** 2026-08-31  
**Owner:** Platform team  
**Related Pillars:** P13 API, Events & Extensibility, P11 Security & Identity  
**Roadmap Question:** `specs/pillars/00-product-roadmap.md:135` Q5  
**Tasks/Features Affected:** FM-004 `T01-004` `task_20355721f4f1`, FM-005 `T02-005` `task_bd788cd29a47`, FM-056 `T05-056` `task_ffd9a3457e2c`, FM-057 `T05-057` `task_1c74e75e3950`, FM-030 `T05-030` `task_1db1a5d27c7b`, `backend/app/interfaces/http/routers/*`, `frontend/src/api/types.gen.ts`

## Context
`AGENTS.md:57` R7 requires `/api/v1/...` with `Page[T]` `plan.md:284`, `Idempotency-Key` `interfaces/http/idempotency.py`, `If-Match` `case.py:156`, `ErrorResponse` `interfaces/http/errors.py`. Roadmap `00-product-roadmap.md:135` and `spec.md:298` state **first slice is internal-only; public API comes with hardening** (P13 US distinction). `specs/001-case-management-platform/first-slice/plan.md:268` versions at URL prefix `/api/v1/...`. Open question is boundary, lifecycle, and compatibility guarantees.

Existing constraint: v1 ships `/api/v1/cases`, `/tasks`, `/documents`, `/audit`, `/admin/*`, `/health` as internal, not yet hardened for external consumers `AGENTS.md:174`.

## Decision Required
Where is the boundary between internal and public contracts for v1, and what compatibility is promised?

## Viable Options

### Option A — Internal-only v1, no public commitment (current constraint)
- `/api/v1/*` is versioned but documented as internal; no `Sunset`/`Deprecation` headers; breaking changes allowed per slice until `T06-REL` hardening.
- **Pros:** Minimum overhead; P1/P2 stories `spec.md:36` can iterate; aligns with deferred public API hardening `00-product-roadmap.md`.
- **Cons:** External consumers cannot rely on contracts; future public hardening will be breaking if not versioned.

### Option B — Single `/api/v1/*` with shared compatibility (one public contract set)
- All endpoints promise additive-only within major (`P13 Q2` `P13/pillar.md:8`); breaking requires `v2` or new type; add `Sunset`/`Deprecation` per `P13 Q3`.
- **Pros:** One set of contracts, simpler docs, OpenAPI `app/main.py:60` is single source.
- **Cons:** Hardening (rate limiting, authz, pagination defaults) must be retrofitted to existing endpoints; risk of leakage via overly permissive payloads.

### Option C — Two contract sets: `/api/v1/internal/*` and `/api/v1/public/*` (dual router)
- Internal routes evolve freely; public routes are subset with strict additive-compat, signed batches `T05-046`, and governance.
- **Pros:** Clean separation; internal iteration not blocked by public compat; external hardening isolated.
- **Cons:** Duplication (two routers, two OpenAPI tags), frontend today uses internal but must not leak public.

## Consequences / Tradeoffs
| Option | Schema/Migration | Public Contract | Tenant Isolation | Event Compatibility | Reversibility |
|---|---|---|---|---|---|
| A | None | None (no promise) | Internal authz only `T01-002` | No version promise; `T03-055` catalog internal | Reversible until public publish; irreversible once external docs shipped |
| B | None | Single compat set, additive-only | Same `T01-002` for all | Requires `CaseCreated.v2` style `P13 Q2` | High — breaking after publish is contractual breach |
| C | Two routers, two OpenAPI files | Dual compat; public additive, internal flexible | Dual policy sets `application/authorization.py` | Public events versioned separately | Medium — can collapse to B later but dual overhead remains |

## Recommended Option
**Option A for v1, explicitly, with Option B as target for post-001 hardening** — i.e., ship v1 as internal-only with clear docs (`docs/TECH_SPEC.md`, `architecture.md`) stating "no public compat yet; v1 contracts are internal", then introduce public compat via Option B after `T06-REL` quality gates green.

## Why Recommended
- Faithful to explicit roadmap constraint `00-product-roadmap.md:135` and `spec.md:298` — do not pretend hardening exists.
- Keeps blast radius small: `T01-004` can ship `ErrorResponse`/`Page` without public `Sunset` overhead `P13 Q3`.
- Allows `T03-055` event catalog to iterate before version freeze; `T05-047` extension registration can harden later.
- Domain unchanged; only `interfaces/http` routing/docs differ.

## Reversibility / Migration Implications
- **Reversible now:** Staying internal has zero migration; switching to B later is additive (add headers, docs, compat tests `T06-TEST`).
- **Irreversible later:** Once a public contract is published (`T06-REL` docs), breaking it requires `v2` and dual support for `Sunset` window per `P13 Q3`; gate this transition.

## Tasks/Features Affected
`T01-004` `task_20355721f4f1`, `T02-005` `task_bd788cd29a47`, `T02-006` `task_bde1270ea0e3`, `T03-055` `task_e22f33b1adcb`, `T05-047` `task_f6e264096540`, `T05-056` `task_ffd9a3457e2c`, `T05-057` `task_1c74e75e3950` (GraphQL), `T06-DOC` `task_a7ca599c1f06`, `T06-REL` `task_9bbe3b587feb`. FM-004, FM-055, FM-056, FM-057, FM-030. No deferred conversion (search engine, etc.).

## Existing Constraint Recorded
Existing constraint in `00-product-roadmap.md:135` — **first slice internal-only** — reaffirmed, not reopened. A future ADR will promote to public with `ADR-004-event-catalog.md`-style compat.

## Approval Gate
Not expensive to reverse in v1; **no dedicated gate required now**. Gate would be created when promoting to public (would block `T06-REL` pre-publish). Current gates `gate_d0c50c578580` (ARCH) and `gate_f86559fe5f17` (REL) already cover.

## Azure Seam
None directly; public hardening would add rate limit + Entra app registration `docs/integrations/seams.md`.

---
*Status stays Proposed until public promotion decision is explicitly gated.*
