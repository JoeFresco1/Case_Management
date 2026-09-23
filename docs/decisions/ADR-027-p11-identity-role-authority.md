# ADR-027 — P11 Identity and Role Authority

**Status:** Accepted — approved via gate `gate_a970381ec29e` on `task_13d3ffcda05d` `T01-002` at 2026-08-31 16:02:35 (`approved`)  
**Date:** 2026-08-31  
**Accepted:** 2026-08-31 (gate `gate_a970381ec29e` → `approved`)  
**Owner:** Platform team  
**Related Pillars:** P11 Security & Identity  
**Pillar Questions:** `specs/pillars/P11-security-identity/pillar.md:8` Q1 (roles from local vs IdP), Q2 (multi-tenant user)  
**Tasks/Features Affected:** FM-002 `T01-002` `task_13d3ffcda05d`, FM-048 `T03-048` `task_4a6adfddbfaf`, FM-001 `T00-001` `task_498206edd308`, `backend/app/domain/role.py:1`, `backend/app/application/authorization.py:1`, `backend/app/deps.py:98`, `backend/app/config.py:18`, `AGENTS.md:67`

## Context
Authz is deny-by-default `application/authorization.py:policy.evaluate` `AGENTS.md:78`, server-side `AGENTS.md:78`, multi-tenant `WHERE tenant_id=:tenant_id` `AGENTS.md:67`. `AGENTS.md:121` says local dev uses `X-Dev-User`/`X-Dev-Tenant` only when `APP_ENV=local` `deps.py:98`; `shared`/`prod` reject `401`. `P11 Q1` asks where roles come from — local config, IdP claim, or both? `P11 Q2` asks can a user belong to multiple tenants, carrying one `tenant_id` at a time and switching with audit?

Existing constraint: `T00-001` already implements `LocalHeaderIdentityProvider` `deps.py:62` seeded users `intake/reviewer/supervisor`; `T01-002` implements `Policy` registry with `UserContext(user_id, tenant_id, roles, is_system)` `deps.py:92`.

## Decision Required
For v1, what is the authoritative source of roles, and can a user span tenants?

## Viable Options

### Option A — Local seeded roles in `APP_ENV=local`; IdP claims in `shared`/`prod`; UserContext single `tenant_id` with audited switch (current, recommended)
- `T00-001` seeds `ACME` tenant roles; `T03-048` `manage_roles.py` CRUD persists roles per tenant; in `prod`, `IdentityProvider` `deps.py:58` maps Entra claims → `roles`; `P11 Q2` yes — user may belong to many tenants but request carries one `tenant_id`, switch is explicit `X-Dev-Tenant` (local) or app-level tenant switch with audit `T04-045`.
- **Pros:** Matches `AGENTS.md:78` fail-closed and `constitution.md V` least-privilege; local dev fully synthetic `example.test` `plan.md:259`; no corporate IdP needed `AGENTS.md:3`; auditable switch.
- **Cons:** Must implement switch audit; `T01-002` `tenant_id` required on every repo method must still be enforced even when user has many tenants.

### Option B — Roles only from IdP claims, no local role store
- `T03-048` becomes read-only projection of IdP; local seed cannot assign roles.
- **Pros:** Single authority.
- **Cons:** Blocks local dev when IdP unavailable `AGENTS.md:3`; `APP_ENV=local` would need Entra mock; `T03-048` FM-048 would be Gated until Azure `AGENTS.md:23`; breaks walking skeleton `tasks.md:31` `T001-T020`.

### Option C — Union: IdP claims plus local overrides
- Effective `roles = IdP_roles ∪ local_roles`.
- **Pros:** Flexibility.
- **Cons:** Ambiguous authority; policy audit `T04-045` must record source; least-privilege harder to reason; `AGENTS.md:78` deny-by-default complicated.

## Consequences / Tradeoffs
| Option | Schema/Migration | Public Contract | Tenant Isolation | Event Compatibility | Reversibility |
|---|---|---|---|---|---|
| A | `roles` + `role_assignments` `task_4a6adfddbfaf` per tenant | `UserContext` single `tenant_id` per request | Strong — `WHERE tenant_id` + switch audit | Events carry `actor_id`+`tenant_id` | Low — can add IdP mapping later without schema |
| B | No local role tables | Same but IdP is sole | Strong but local dev blocked | Same | Medium — adding local store later is migration |
| C | Both stores | Same but ambiguous | Medium — union may leak | Same but audit must store source | Hard — removing union requires reconciliation |

## Recommended Option
**Option A — Local seeded + IdP claims per env; single `tenant_id` per request with audited switch.**

## Why Recommended
- Preserves explicit `AGENTS.md:78` `APP_ENV=local` vs `prod` split `deps.py:98` and synthetic only `plan.md:259`.
- Keeps `T01-002` tenant isolation testable with `sqlite+aiosqlite` `AGENTS.md:121` and 2-tenant parametrized tests `AGENTS.md:153`.
- Enables `T03-048` FM-048 role admin operable locally before Azure `AGENTS.md:23` blocked; `T04-045` access audit `task_9ca345015183` can log switch.
- Domain `UserContext` stays simple (one tenant at a time) `AGENTS.md:67`; cross-tenant is explicit break-glass `task_33be46331ffd` `FM-035`.

## Reversibility / Migration Implications
- **Reversible now:** Switching role source is wiring in `config.py`/`deps.py`/`main.py:56` DI `AGENTS.md:30`; domain unchanged.
- **Irreversible later if B/C chosen early:** Local seeds would be lost or ambiguous; existing `role_assignments` rows would need mapping to IdP claims; break-glass `FM-035` stitching would need claim re-interpretation. Gate before `T01-002` publish.

## Tasks/Features Affected
`T00-001` `task_498206edd308` (seed), `T01-002` `task_13d3ffcda05d` (Policy, tenant isolation, `deps.py` switch), `T03-048` `task_4a6adfddbfaf` (role admin), `T04-035` `task_33be46331ffd` (cross-tenant switch), `T04-045` `task_9ca345015183` (decision audit), `T06-SEC` `task_9923f0574bc9`. FM-001, FM-002, FM-048, FM-035, FM-045.

## Approval Gate
**Created:** `gate_P11_identity` on `task_13d3ffcda05d` `T01-002` (see report). Blocks authorization foundation until authority model decided. Alternative choke `T00-001` but `T01-002` is execution point where `policy.evaluate` is wired.

## Existing Constraint Recorded
Existing constraints: `APP_ENV=local` header `LocalHeaderIdentityProvider` `AGENTS.md:78`, deny-by-default `AGENTS.md:78`, `UserContext` single tenant `AGENTS.md:67` — reaffirmed, not reopened.

## Azure Seam
`IdentityProvider` `domain/ports.py` `LocalHeaderIdentityProvider` `deps.py:62` → `EntraIdentityProvider` `docs/integrations/seams.md` `AGENTS.md:180`; roles mapping is adapter concern, not domain.

---
*Status Accepted via gate `gate_a970381ec29e` (`approved`). Do not revert without superseding ADR.*
