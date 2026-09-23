# ADR-010 — Versioned Extension and Event-Type Registration

**Status:** Accepted  
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P13 API, Events & Extensibility; P09 Audit; P10 Administration; P11 Security  
**Pillar Question:** P13-US12 / FM-047 (versioned extension registration)  
**Tasks/Features Affected:** `task_f6e264096540` `T05-047`; `backend/app/domain/extension.py`; `backend/app/application/extensions/register_extension.py`; `backend/app/interfaces/http/routers/extensions.py`; `backend/tests/extensions/test_registration.py`

## Context

`specs/pillars/P13-api-events-extensibility/pillar.md:124` (P13-US12) requires that a module developer can extend the platform with a module that registers new event types and new policy actions, and that the registration is itself **versioned and audited**. The deduplicated feature map (`specs/pillars/01-deduplicated-feature-map.md:106` FM-047) places this as an H3 outcome for P13 with supporting ownership from P09 (audit), P10 (administration), and P11 (security).

Today the platform exposes:

- A built-in `EVENT_CATALOG` in `backend/app/domain/events.py:154` of four entries (`CaseCreated`, `TaskCompleted`, `DocumentAdded`, `WorkflowTransitioned`), declared at code-load time.
- A `Action` enum in `backend/app/application/authorization.py:56` whose values are hard-coded; new actions require a code change.
- An audit pipeline (`backend/app/domain/audit.py`, `application/audit/*`) that records every state-changing action.

None of these are extensible at runtime; adding a new event type today requires patching `domain/events.py`, opening a PR, and shipping a new release. The same applies to policy actions. Two constraints from the AGENTS.md / plan bind the solution:

1. Audit rows must be tamper-evident (Constitution XIII.5; AGENTS.md §4.R12). A registration that fails to commit its audit row leaves a phantom install.
2. PHI must not leak via event payloads (plan §259; AGENTS.md §170). A registration that declares PHI-named payload keys is a footgun the operator must catch before the first publish.

## Decision Required

How does a developer (today) or a tenant-signed artifact (future) register a versioned extension module that contributes new policy actions and new event types, while remaining versioned, governed, and tenant-isolated?

## Viable Options

### Option A — In-process extension registry with audit (recommended)

A `ExtensionRegistry` lives in the application layer; `register_extension` validates the spec end-to-end, installs it under a thread-safe lock, and appends an `ExtensionRegistered` audit row carrying the full spec so the registry is reproducible from the audit chain.

- **Pros:** Zero new infrastructure (no schema migration; no Azure resource); same authorization gate (`ADMIN_TENANT_CONFIG`) as role/workflow publication; the audit row is the durable source of truth; idempotent retry of an identical `(module_id, version)` is a no-op; a different shape at the same key raises `409 Conflict`. Tests run against the sqlite profile (AGENTS.md §4.R1).
- **Cons:** Process-local state — restarting the app loses the registry unless a future slice replays from the audit chain (FM-047 slice 2). Tenant isolation is partial: a registration in tenant A is visible to tenant B at the catalog endpoint today (see Consequences).

### Option B — Persistent `extension_registry` table with row-level versioning

Add a SQL table for `ExtensionSpec` rows; the registry reads on boot and replays audit-verified installs.

- **Pros:** Restart-safe; first-class tenant isolation via `WHERE tenant_id = :tenant_id`; SQL adapters match every other repository.
- **Cons:** Adds a migration and a new repository; conflicts with the "FM-047 H3, deferred" horizon; the in-process approach is enough to validate the public contract before the persistent shape is locked in.

### Option C — Code-only declarations (no registry)

Move the extension surface into source files under `backend/app/extensions/<module>.py` and import them at startup.

- **Pros:** Simple; no new tables; no audit row.
- **Cons:** Defeats P13-US12 ("registration is itself versioned and audited"); no tenant-defined audit types; no runtime governability. Rejected.

## Consequences / Tradeoffs

| Option | Schema/Migration | Public Contract | Audit Chain | Tenant Isolation | Reversibility |
|---|---|---|---|---|---|
| A | None | Versioned, namespaced | Same-transaction row | Partial (catalog-level) | Easy (remove audit row + replay exclusion) |
| B | `extension_registry` migration | Versioned, namespaced | Same-transaction row | Strong | Harder (must reconcile persistent + audit) |
| C | None | Code-only | None | n/a | Trivial (revert) |

### Consequence — Tenant isolation gap (acknowledged)

The first-slice registry is keyed on `(module_id, version)` only — not on `tenant_id`. A registration in tenant A is **also visible** to tenant B in the catalog response (`GET /api/v1/extensions`) and at the policy evaluator. The gap is acceptable for two reasons:

1. FM-047 is H3 in the deduplicated map. The first slice ships the **mechanism** (registry + validation + audit + versioning) so the catalog endpoint and use cases can be reviewed; per-tenant scoping is a property of the persistent table from Option B.
2. The HTTP boundary already requires `ADMIN_TENANT_CONFIG`, which is a tenant-admin grant. A cross-tenant observation does not, today, expose data the actor could not already learn through the policy admin surface.

The ADR records the gap; the FM-047 slice 2 work item closes it by either (a) keying the registry on `(tenant_id, module_id, version)` or (b) introducing the persistent table.

### Consequence — Audit-event reconciliation

`RegisteredModule.audit_event_id` carries a placeholder UUIDv7 today; the SQL `AuditEventStore.append` returns `None` and the row id is allocated server-side. A future slice returns the row id from `append` and back-fills `RegisteredModule.audit_event_id`; the ADR defers the reconciliation until then (no functional regression — the audit row itself is durable, and a future replay CLI resolves the mapping by `(tenant_id, event_type, correlation_id, payload_hash)`).

## Recommended Option

**Option A — In-process extension registry with audit.**

## Why Recommended

- Matches the dispatch acceptance criteria: a developer registers a new event type versioned; a tenant-defined audit type is governed and appears in the catalog; duplicate versions are rejected.
- Mirrors the existing pattern for role and workflow publication (`backend/app/application/roles/manage_roles.py`, `backend/app/application/workflow/publish_workflow.py`) — same authorization gate, same audit row shape, same idempotent-retry semantics.
- Validates the public contract (Pydantic schemas, OpenAPI tags, error model) before the persistent table is locked in. Slice 2 only has to swap the registry implementation; the wire surface stays.
- Keeps the first slice under the "no Docker" CI profile (AGENTS.md §4.R1): tests use the in-process registry and the sqlite profile, so the gate is fast and deterministic.

## Reversibility / Migration Implications

- **Before any registered extension is in production:** Reversible by removing the registry, the use case, the router, and the tests; no audit rows are written so the chain is unaffected.
- **After an install lands on the chain:** Removing the audit row would break the hash chain (AGENTS.md §4.R12 / plan §252). Reversal requires an `AuditCorrectionRecorded` row pointing at the install row and a process restart that excludes the install during replay. The persistent-table slice 2 introduces a `retired_at` column so retirement does not require touching the chain.
- **Migration to Option B:** The slice 2 work item introduces the persistent table, replays every install via the audit chain at first boot, and demotes the in-process registry to a read-through cache. The use case's audit row is the replay source of truth.

## Tasks/Features Affected

`T05-047` `task_f6e264096540` (extension registration); future `T05-047-slice-2` (persistent table + replay); `T03-055` `task_e22f33b1adcb` (built-in catalog — touched only by the union endpoint).

## Existing Constraint Recorded

- P13 §7 places FM-047 as a P3 / H3 outcome — recorded as **deferred-now, foundation-now** so the platform supports extension-shaped features without claiming H3 stability.
- Plan §259 / AGENTS.md §170 prohibit PHI in event payloads — encoded in the validator (`_PHI_KEY_NEEDLES`).
- ADR-023 (additive-only within major, breaking -> new type version) — extensions must declare a version and a payload-key whitelist; breaking changes bump the `version` field (FM-047 slice 2 formalizes the rule).

## Approval Gate

Recorded under `task_f6e264096540`; the gate is the dispatch's `worker_done` event. No separate operator gate — the use case enforces `ADMIN_TENANT_CONFIG` and the audit row is the durable decision record.

## Azure Seam

`ExtensionRegistry` is process-local today. The Azure adapter replaces it with a `SqlExtensionRegistry` that reads the persistent table on boot and replays audit-verified installs; the Protocol in `backend/app/domain/extension.py` (`ExtensionRegistry.install` / `list_modules` / `list_event_specs` / `find_event_spec` / `has_action`) is the seam. `docs/integrations/seams.md` should add an entry under the **Extensibility** row.

---

*Status Accepted via `worker_done` of `task_f6e264096540`. Do not extend the in-process registry beyond module / action / event-type registration without superseding this ADR; the slice 2 work item is the natural place to add replay / retirement semantics.*