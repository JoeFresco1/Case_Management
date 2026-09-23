# ADR-023 — Event Compatibility and Versioning

**Status:** Accepted — approved via gate `gate_da5af2fd3999` on `task_e22f33b1adcb` `T03-055` at 2026-08-31 16:17:09 (`approved`)  
**Date:** 2026-08-31  
**Accepted:** 2026-08-31 (gate `gate_da5af2fd3999` → `approved`)  
**Owner:** Platform team  
**Related Pillars:** P13 API, Events & Extensibility  
**Pillar Question:** `specs/pillars/P13-api-events-extensibility/pillar.md:8` Q2  
**Tasks/Features Affected:** FM-055 `T03-055` `task_e22f33b1adcb`, FM-056 `T05-056` `task_ffd9a3457e2c`, FM-047 `T05-047` `task_f6e264096540`, FM-014 `T02-014` `task_139bd42578fb`, `backend/app/domain/events.py:1`, `backend/app/infrastructure/messaging/outbox_dispatcher.py:1`

## Context
Outbox is the sole source of integration events `plan.md:102`, `constitution.md VIII`. `AGENTS.md:180` requires versioned `CaseCreated.v2` style if breaking. `specs/001-case-management-platform/first-slice/plan.md:182` lists `CaseCreated`, `CaseUpdated`, `WorkflowTransitioned`, `DocumentAdded`, `DecisionRecorded`, `TaskCreated`, `TaskCompleted`, `AuditCorrectionRecorded` as domain events with `event_type`, `event_id`, `payload_json`, `correlation_id`, `causation_id`. No corporate Service Bus in v1 `AGENTS.md:23`.

Existing constraint: `spec.md:298` says API `v1` is internal-only; event catalog is documented `T03-055` but not yet public-hardened. `AGENTS.md:83` requires chain-of-custody but not yet SIEM signing `T05-046`.

## Decision Required
Within a major version (e.g., `CaseCreated.v1`), are payloads additive-only or freely evolving? How are breaking changes introduced?

## Viable Options

### Option A — Additive-only within major, new type version for breaking (recommended in pillar)
- New fields optional; removal/rename/type change → new `event_type` `CaseCreated.v2` with distinct `version`; consumer subscribes to both; `T06-INT` placeholder `AzureServiceBus` delivers both.
- **Pros:** Consumer safe; replayable; aligns with `P13 Q3` `Sunset`/`Deprecation`; no silent break.
- **Cons:** More types; publisher must maintain dual dispatch until `Sunset` window; `outbox` stores `event_type`+`version`.

### Option B — Free evolution within major (no compat promise)
- Payload may change without notice; consumer breakage is consumer’s problem.
- **Pros:** Publisher freedom; single type.
- **Cons:** Breaks integration `T05-058`/`T05-059` `P14`; violates Constitution VII (contracts are versionable); WSL-untestable downstream breakage; irreversible once external subscriber exists.

### Option C — Single payload with `schema_version` field, breaking indicated by major bump in same type
- One `CaseCreated` with `payload.schema_version = 2`; consumer branches on field; deliverer filters.
- **Pros:** Single type, less catalog sprawl.
- **Cons:** Consumer must inspect field; catalog `GET /api/v1/events/catalog` must version per payload; harder to deprecate per-type; `T05-047` tenant-defined types conflict.

## Consequences / Tradeoffs
| Option | Schema/Migration | Public Contract | Event Compatibility | Tenant Isolation | Reversibility |
|---|---|---|---|---|---|
| A | New type version is migration (dual write) but additive | Additive promise, `v2` explicit | Strong — new version is new contract | None | Medium-hard to reverse after subscribers migrate to v2 |
| B | None | No promise | Weak — silent break | None | Irreversible after publish (breakage) |
| C | Field version is schema change | Single type but field-versioned | Medium — branching logic | None | Medium — collapsing to A later is refactor |

## Recommended Option
**Option A — Additive-only within major, `*.v2` for breaking.**

## Why Recommended
- Matches explicit pillar intent `P13/pillar.md:8` Q2 "additive changes only within a major version; breaking changes require a new event type version" — record, not reopen.
- Enables replayable outbox `T03-052` `T01-003` without loss; `correlation_id` `deps.py:45` preserved per type version.
- Keeps domain `domain/events.py` agnostic of transport `AGENTS.md:75`; only `infrastructure/messaging` maps to Service Bus `docs/integrations/seams.md`.
- Allows `T05-047` tenant-defined audit types to be additive without core break.

## Reversibility / Migration Implications
- **Before subscriber:** Reversible by ADR supersede before `T06-REL` publish.
- **After subscriber:** Irreversible without `Sunset` window `P13 Q3` (≥6 months `Deprecation` `RFC 9745`); dual dispatch drains via `outbox_dispatcher.py` retry; consumer must support both `v1`/`v2` until cutoff. Gate this before `T03-055` completes to avoid expensive dual support.

## Tasks/Features Affected
`T03-055` `task_e22f33b1adcb` (catalog), `T02-014` `task_139bd42578fb` (publishes), `T05-056` `task_ffd9a3457e2c` (outbound webhooks distinct `FM-030` vs `FM-056`), `T05-047` `task_f6e264096540` (extension registration), `T06-INT` `task_279162523b18`, `T06-REL` `task_9bbe3b587feb`. FM-055, FM-056, FM-047, FM-014.

## Existing Constraint Recorded
Existing constraint in `P13/pillar.md:8` Q2 — **additive within major** — reaffirmed, not reopened. A breaking change without `v2` would be a violation.

## Approval Gate
**Created:** `gate_P13_event_compat` on `task_e22f33b1adcb` `T03-055` (see report). Blocks event catalog until approved; `T02-014` is upstream but gate on `T03-055` is cheapest choke point before public exposure.

## Azure Seam
`MessageBus` `domain/ports.py` local `local_message_bus.py` → `AzureServiceBus` `infrastructure/messaging/azure_servicebus_*` `AGENTS.md:180`; version routing in adapter, not domain.

---
*Status Accepted via gate `gate_da5af2fd3999` (`approved`). Do not revert without superseding ADR and Sunset migration.*
