# ADR-016 — Truthful Coexistence Policy: Source References, Ownership, Migration State (FM-059)

**Status:** Accepted (Gated)
**Date:** 2026-09-02
**Owner:** Platform team
**Related Pillars:** P14 Integrations & Migration, P01 Case & Workspace, P09 Audit & Evidence, P10 Workflow & Configuration, P13 API & Contract
**Pillar Questions:** `specs/pillars/P14-integrations-migration/pillar.md` US6, §2 (In Scope — Coexistence), §8 Q4
**Tasks/Features Affected:** FM-059 (`specs/pillars/01-deduplicated-feature-map.md:123`); `backend/app/domain/coexistence.py:1`; `backend/app/application/migration/coexistence.py:1`; `backend/app/interfaces/http/routers/migration.py:1`; `backend/app/domain/ports.py` (`CoexistencePolicyRepository`); `backend/tests/migration/test_coexistence.py:1`; AGENTS.md §3 R12, §4 R8, §12.

## Context

Pillar P14 (`specs/pillars/P14-integrations-migration/pillar.md`) commits to incremental migration from legacy systems **without cloning or depending on legacy**. A migrated case carries source-system references, ownership, and migration state (P14 §2 In Scope — Coexistence). P14-US6 (P3 / FM-059) is the user story: "As a process administrator, I can define a coexistence policy: a case carries source-system references, ownership, and migration state; the platform does not pretend unapproved real-time sync exists."

The hard rule is constitutional, not aspirational. Constitution V.10 / XII.5: **no connector to OnBase, Panzura, or any enterprise system is enabled without explicit authorization, approved credentials, approved network access, and an approved integration design.** The pillar reaffirms this in §1: "A connector cannot be enabled merely because a URL or credential is present." And §2's Out of Scope: "Real-time bidirectional sync with a legacy system. (P3; v1 supports one-way, batched migration only.)"

The risk the pillar names is structural. If the platform surfaces a `last_sync_at` field, or a `sync_status` enum with a `LIVE` value, or an outbound change-log, **the field implies a channel that does not exist**. An operator reading the operator console would infer the platform is in active contact with legacy when it is not — a Constitution V violation dressed up as an observability feature.

Three questions are not answered by the pillar:

1. **What is on the policy row?** A policy is governance, not case-level state. It must be one document an auditor can read end-to-end and answer "what did this tenant declare about legacy coexistence?". The row must surface source references, ownership rule, migration state, and the **declared sync channel** (with the truthful default of OFF).
2. **How does the policy prevent implying an unapproved sync?** Two layers, mirroring ADR-015. The application layer's authorization is the policy half — it refuses a request with `real_time_sync_enabled=True` because no role grants the corresponding action today. The domain's `__post_init__` is the deterministic backstop — it refuses to construct a row with `real_time_sync_enabled=True` unless `sync_authorized_by` is set, regardless of caller. The two checks must agree.
3. **What does "LIVE" mean?** It is not a state in this model. A case is "live in this platform" by virtue of NOT being `divergent` and NOT being `unmigrated`. Encoding `LIVE` as a state would let the policy claim a positive assertion ("this case is active") — that is the legacy system's vocabulary, not this one's. The closed `MigrationState` enum is `unmigrated | imported | reconciled | divergent | retired_from_source` and nothing else.

## Decision

The coexistence model is implemented as four layers plus two Gated authorization surfaces:

### 1. Domain aggregate (`backend/app/domain/coexistence.py`)

`CoexistencePolicy` is a frozen, tenant-scoped aggregate:

- **Identity**: `id`, `tenant_id`, `key` (operator-facing), `version` (monotonic).
- **Truthful declaration**: `source_systems: tuple[str, ...]` (closed set of named legacy systems), `ownership: OwnershipRule`, `migration_state: MigrationState`, `real_time_sync_enabled: bool` (default `False`).
- **Provenance**: `provenance_note` (free-text describing where this policy came from), `created_at` / `created_by` / `updated_at`, `sync_evidence: tuple[UUID, ...]` (append-only import-job ids).
- **Gated authorization records**: `sync_authorized_by` / `sync_authorized_at` (required iff `real_time_sync_enabled=True`), `ownership_authorized_by` / `ownership_authorized_at` (required iff `ownership=SHARED`).

The aggregate's `__post_init__` enforces three Gated rules structurally (no caller can bypass them):

- `real_time_sync_enabled=True` requires `sync_authorized_by` AND `sync_authorized_at`. The default value of `real_time_sync_enabled` is `False` and the constructor does not set the authorization records; the first slice ships sync OFF.
- `real_time_sync_enabled=False` with a stray `sync_authorized_by` record is refused — keeps the audit trail clean ("sync is OFF and there is no authorization record" is one row, not "sync is OFF but here is an authorization no one is acting on").
- `ownership=SHARED` requires `ownership_authorized_by` AND `ownership_authorized_at`. `SHARED` implies a bidirectional write channel that does not exist in the first slice; today `LEGACY_SYSTEM_OF_RECORD` and `THIS_PLATFORM_SYSTEM_OF_RECORD` are the only admissible values.

`MigrationState` is a closed enum: `UNMIGRATED`, `IMPORTED`, `RECONCILED`, `DIVERGENT`, `RETIRED_FROM_SOURCE`. `LIVE` is intentionally absent. The `set_migration_state` method allows forward transitions plus the `RECONCILED ↔ DIVERGENT` oscillation; `RETIRED_FROM_SOURCE` is terminal.

The `record_import_evidence` method appends an import-job id to `sync_evidence` (idempotent on duplicate ids) and bumps `UNMIGRATED → IMPORTED` on the first evidence. There is no `last_sync_at` field; the evidence tuple IS the audit trail.

### 2. Application use cases (`backend/app/application/migration/coexistence.py`)

Use cases mirror the connector lifecycle pattern (ADR-015):

- `define_coexistence_policy` — drafts a v1 policy. Refuses unrecognized `source_systems`; refuses duplicate keys (per-tenant uniqueness). Emits `CoexistencePolicyDrafted` to audit + outbox.
- `record_import_evidence` — appends an import-job id. Idempotent on duplicate ids (an operator retry must not produce a second audit row). Emits `CoexistenceImportEvidenceRecorded`.
- `set_migration_state` — operator-driven state transitions. Emits `CoexistenceMigrationStateChanged`.
- `get_coexistence_policy` / `list_coexistence_policies` — tenant-scoped reads; cross-tenant returns `None` → `NotFound`.

Authorization: every entry point requires `ADMIN_TENANT_CONFIG`. The Gated `real_time_sync_enabled=True` path requires the corresponding (currently non-existent) authorization action; denied by default today. The application layer's check is the policy half; the domain's `__post_init__` is the deterministic backstop.

### 3. Persistence port (`backend/app/domain/ports.py`)

`CoexistencePolicyRepository(TenantScoped, Protocol)` with `get`, `get_by_key`, `get_latest_version`, `list`, `add`, `update`. Every method takes `tenant_id` first; the SQL adapter MUST include `WHERE tenant_id = :tenant_id` on every query (AGENTS.md §3 R1). The first slice ships an in-memory repository at the application layer; the SQL adapter lands alongside the first named coexistence policy authorization.

### 4. HTTP router (`backend/app/interfaces/http/routers/migration.py`)

Endpoints (all under `/api/v1/admin/coexistence`):

- `POST   /api/v1/admin/coexistence`                       — define a v1 policy.
- `GET    /api/v1/admin/coexistence`                       — list policies.
- `GET    /api/v1/admin/coexistence/{id}`                  — read one policy.
- `POST   /api/v1/admin/coexistence/{id}/import-evidence`  — append import-job id.
- `POST   /api/v1/admin/coexistence/{id}/migration-state`  — advance migration state.

Every endpoint requires `ADMIN_TENANT_CONFIG`. The `POST /admin/coexistence` endpoint refuses `real_time_sync_enabled=True` with `403`; the operator console reads `real_time_sync_enabled=False` as the truthful default and `provenance_note` for the operator-written source-of-truth citation.

### 5. Gated authorization surfaces

- `real_time_sync_enabled=True`: today no role grants the corresponding action; the application layer's HTTP route returns 403 and the domain refuses the row construction. A future ADR-XXX authorizes the action only after the platform has a real-time channel with a named legacy system (today it does not, per Constitution V.10 / XII.5 / P14 §1).
- `ownership=SHARED`: same pattern. Today no role grants the corresponding action; the application layer refuses the request and the domain refuses the row construction.

### 6. Why no `LIVE` state

A `LIVE` value in the `MigrationState` enum would let the policy claim a positive assertion about a case — "this case is active in this platform". The platform's actual claim is: "this case exists here AND is not divergent AND is not unmigrated". Encoding that as a state would require recomputing the value on every read; encoding it as a property (the negative form) is what `MigrationState` actually expresses. A `LIVE` value is therefore reserved for a future ADR if the platform ever gains the ability to make a positive assertion — e.g. after the named connector authorization lands and the platform can answer "this case was last reconciled 5 minutes ago".

## Consequences

### Positive

- The platform can ship the coexistence row today with synthetic-only data (AGENTS.md §18). The operator console reads `source_systems`, `ownership`, `migration_state`, and `sync_evidence` and answers "what does this tenant declare about legacy?" without lying.
- An auditor reading `audit_events` sees `CoexistencePolicyDrafted` / `CoexistenceImportEvidenceRecorded` / `CoexistenceMigrationStateChanged` and reconstructs the policy's lifecycle.
- The Gated real-time sync / SHARED-ownership paths have two independent gates (application + domain) — a misconfigured HTTP route cannot silently enable a sync channel that does not exist.

### Negative

- A future operator who wants `real_time_sync_enabled=True` cannot get it today, even with a role that grants every other admin action. The denial is structural, not a UX bug.
- The closed `source_systems` set is currently `{synthetic-legacy, onbase, panzura, sharepoint}`. Adding a new label is a product decision; the application layer rejects the request and the policy is the place to record the addition.

### Neutral

- The aggregate has no `last_sync_at` field, no `sync_status` field, no `outbound_change_log`. The absence is the design. A future feature that adds any of these must come with a separate ADR recording "the platform now has a real-time channel X with legacy Y" — the absence of an ADR means the field is forbidden.

## Azure-seam note

The Azure migration target inherits this model verbatim. A future `infrastructure.persistence.azure_coexistence_policy_repository` implements `CoexistencePolicyRepository` against Azure SQL with portable types (AGENTS.md §3 R1). No field on the aggregate changes. The `real_time_sync_enabled` flag remains the truthful declaration of "the platform has a real-time channel"; setting it to `True` in Azure requires the same authorization action that is Gated in local. The seam is documented in AGENTS.md §12.