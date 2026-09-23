# ADR-004 — Event Catalog (FM-055 / P13-US8)

**Status:** Accepted  
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P13 API, Events & Extensibility  
**Pillar Question:** `specs/pillars/P13-api-events-extensibility/pillar.md:8` Q2  
**Tasks/Features Affected:** FM-055, `T03-055`, `task_e22f33b1adcb`  
**Supersedes:** none  
**Superseded by:** none

## Context

The first slice (`001-case-management-platform`) ships the API surface
and a minimal event catalog. FM-055 promotes the catalog from a
documented list (`specs/001-case-management-platform/first-slice/plan.md:182`)
to a **runtime source of truth** that publishers, subscribers, and the
`/api/v1/events/catalog` endpoint all read from the same in-code
declaration. ADR-023 already settled the additive-only / `*.v2`
versioning policy; this ADR records the concrete shape of the catalog
in code and the rules around it.

Constraints being honored:

- Outbox is the sole source of integration events
  (`plan.md:102`, `constitution.md VIII`).
- `MessageBus` lives behind a `Protocol` so the local in-process
  adapter is swappable for `AzureServiceBus` (AGENTS.md §12).
- The synthetic marker, tenant isolation, and correlation/causation
  ids are first-class on every published event.
- PHI is forbidden by payload shape: events reference sensitive values
  by id, never by content (Constitution V, plan §18).

## Decision

The first-slice catalog contains four event types, all at version 1:

| `event_type`           | Trigger                                          | Payload keys (whitelist) |
|---|---|---|
| `CaseCreated`           | `case_create` use case commit                     | `case_id, reference, case_type_key, tenant_id, actor_id, occurred_at` |
| `TaskCompleted`        | `task_complete` use case commit                   | `task_id, case_id, tenant_id, actor_id, outcome, occurred_at` |
| `DocumentAdded`        | `document_upload` / `document_version_add` commit | `document_id, case_id, tenant_id, actor_id, sha256, classification, version_number, occurred_at` |
| `WorkflowTransitioned` | `workflow_transition` use case commit             | `workflow_instance_id, case_id, tenant_id, actor_id, from_state, to_state, transition_key, workflow_version, occurred_at` |

The catalog is declared in `app/domain/events.py` as an ordered tuple
of `EventTypeSpec` dataclasses. The `DomainEvent` envelope validates
every construction against the whitelist. The outbox dispatcher
re-checks the catalog before publishing and dead-letters any row with
an unknown `event_type`.

## Viable Options

### Option A — In-code catalog + runtime whitelist (chosen)
- Single tuple in `app/domain.events.EVENT_CATALOG`; `validate_payload`
  enforces the whitelist at `DomainEvent` construction; the dispatcher
  re-checks at publish time.
- **Pros:** one source of truth; cannot drift between docs and code;
  CI catches a use-case typo at test time; no schema migration.
- **Cons:** requires a redeploy to add an event type; tenant-defined
  event types (FM-047) need a registration overlay.

### Option B — Database-backed registry
- `event_types` table seeded at startup; publishers look up the spec.
- **Pros:** adds are data, not code; tenant-defined types fall out
  for free.
- **Cons:** introduces a query on the hot path; catalog discoverability
  is now a migration story; out-of-band edits become a divergence
  vector. Deferred to FM-047.

### Option C — OpenAPI-only catalog
- Document events in the OpenAPI schema; no code-time enforcement.
- **Pros:** matches API-doc conventions.
- **Cons:** OpenAPI is not the wire format the bus speaks; subscribers
  cannot validate without a separate registry; no compile-time
  guarantee of payload-key conformance.

## Consequences / Tradeoffs

| Option | Schema/Migration | Public Contract | Event Compatibility | Tenant Isolation | Reversibility |
|---|---|---|---|---|---|
| A (chosen) | None (in-code) | Catalog endpoint = `/api/v1/events/catalog` | Strong — additive-only within major | Payload references ids only; no raw PHI | Easy: edit catalog, bump version, add `*.v2` for break |
| B | New table + seed migration | Catalog from DB | Strong, with overlay risk | Same | Harder: requires migration to roll back |
| C | None | OpenAPI only | Weak — silent drift | Same | Trivial |

## Why Recommended

- Aligns with ADR-023's additive-only / `*.v2` policy without
  introducing schema work.
- Honors Constitution XIII (vertical slices, no horizontal infra) by
  shipping a working catalog today, with the DB-backed registry
  deferred to FM-047 where the tenant-defined types pull it.
- Keeps the domain layer pure (AGENTS.md §16.1, `import-linter`) — the
  catalog is pure Python with no I/O.

## Reversibility / Migration Implications

- Reversible by editing the tuple and redeploying before any subscriber
  consumes `*.v2`. After a subscriber is live, breaking changes
  require a `Sunset` window (P13 Q3, ADR-023).
- The outbox dispatcher dead-letters any row whose `event_type`
  falls out of the catalog (e.g. on a downgrade) so no event is lost
  in flight.

## Tasks/Features Affected

`T03-055` (`task_e22f33b1adcb`) — event catalog
implementation. FM-052 (`T03-052`) feeds the dispatcher. FM-056
(`T05-056`) is the webhook surface that reads the same catalog.

## Approval Gate

Inherits the gate from ADR-023 (`gate_da5af2fd3999` `approved`). No
new gate required: this ADR records the implementation of an already
approved policy.

## Azure Seam

`MessageBus` (`app.domain.ports`) local adapter =
`infrastructure/messaging/local_message_bus.py`. Production adapter
will be `infrastructure/messaging/azure_servicebus.py`. The catalog
itself is adapter-agnostic — version negotiation belongs in the
adapter, not in the domain.

---
*Status Accepted. Implements ADR-023. Do not reopen versioning policy without superseding both ADRs.*