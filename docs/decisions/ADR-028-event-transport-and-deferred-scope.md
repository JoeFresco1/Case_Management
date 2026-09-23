# ADR-028 — Deferred Scope and Event Transport (Realtime, Notifications, Search Engine, E-sign/Scan, Standalone Rules)

**Status:** Proposed — intentionally deferred, no gate (record of constraint)  
**Date:** 2026-08-31  
**Owner:** Platform team  
**Related Pillars:** P04 Capture, P06 Search, P13 Events, P10 Admin, Roadmap `00-product-roadmap.md:130` Q2/Q3  
**Tasks/Features Affected:** `T06-REL` `task_9bbe3b587feb` deferred check only

## Context
Roadmap `00-product-roadmap.md:130` and `plan.md:344` defer: real-time WebSocket/SSE transport, notification delivery channels (email/SMS/fax/portal), full-text search engine (Azure Cognitive Search), e-signature/fax/scan `T05-029`, standalone `/admin/rules` CRUD. Instruction in this pass: *Do not convert intentionally deferred scope into implementation work. Keep these deferred unless required by a current dependency*.

Existing constraints: Outbox is sole integration source `T03-052` `task_9891c571b04c` `plan.md:102`; search starts with portable `LIKE` `AGENTS.md:35` `T03-031` `task_1c5e873fcd64`; scan is Gated `T05-029` `task_c8155d70cc6e`; rules are embedded `workflow_version.definition_json.guard` `plan.md:211` `T02-014` `task_139bd42578fb`.

## Decision Required
Should any deferred transport/channel/engine be implemented now to satisfy a current FM dependency?

## Viable Options (for each deferred item)

- **Real-time (WS/SSE):** Option A deferred (outbox only) vs Option B implement SSE now. Current dependency: none — `T02-014`/`T03-055` do not require push; polling via `GET /timeline` `task_704e0aa8e4c9` suffices.
- **Notification channels:** Option A intents only (`T02-SEAM` `SlaWarningRaised`) vs B live email/SMS. No current FM requires delivery; `constitution.md 135` says intents.
- **Search engine:** Option A `LIKE` vs B Azure Cognitive Search adapter `T05-034`. `T03-031` explicitly portable `LIKE`; engine is `P06` H3 `HORIZON` `01-deduplicated-feature-map.md:32`.
- **E-sign/scan:** Option A quarantine Gated `T05-029` vs B real device. No corporate device `AGENTS.md:3` Issue #3 WSL flaky.
- **Standalone rules CRUD:** Option A rules embedded `T02-014` vs B `/admin/rules` `T03-021` separate. `plan.md:344` says standalone is separate future spec.

## Recommended Option
**Keep all five deferred as constraints; no implementation in this pass.** Recorded, not reopened.

## Why Recommended
- No FM in `01-deduplicated-feature-map.md:158` pull order requires them before H1; H1 after `001` is `FM-048`→`FM-011`→`FM-021`→`FM-031`, none needs realtime/search engine.
- Implementing would convert horizontal infra before vertical slice `constitution.md XIII`, violating sponsor `AGENTS.md:1` incremental replacement.
- Local dev has no Azure `AGENTS.md:23` and no Docker Desktop `AGENTS.md:28`; adapters would be contracts only `AGENTS.md:180` with no testable value.

## Reversibility / Migration Implications
Deferred implies **zero migration cost now**; activating later is additive behind `MessageBus`/`SearchProvider`/`DocumentStore` ports `AGENTS.md:180`. If later needed, gate and ADR would block only that slice's first task (e.g., WS would gate a new `T05-WS`).

## Tasks/Features Affected
None newly blocked. `T06-REL` `task_9bbe3b587feb` remains convergence point; deferred ADRs are informational.

## Decisions That Can Safely Remain Unresolved
All five: no implementation consequence today. They are **no-consequence deferred** until a slice explicitly pulls them (search engine until `FM-034` semantic already has `LIKE` fallback; realtime until `P12` dashboard `T05-054` needs sub-5s `task_81bd0ccfb889` — still pollable).

## Existing Constraint Recorded
Reaffirmed: `00-product-roadmap.md:130` deferred list, `plan.md:344` deferred decisions, `AGENTS.md:180` seam placeholders.

---
*No gate. Status Proposed deferred. Will not be marked Accepted until a future slice explicitly pulls the scope.*
