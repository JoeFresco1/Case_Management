# ADR-047: H3 gate authorizations — named connectors deferred, coexistence and export scoped

- Status: Accepted (2026-09-22)
- Scope: FM-058 (named connectors), FM-059 (coexistence/migration), FM-060 (portable evidence
  bundles) — the three H3/Gated rows decided by Run H after Run G converged.
- Source: Run H gate tasks H-058 / H-059 / H-060 (`run_e5801c8d`), each completed with no source
  changes; this ADR is their durable record.
- Related: FM-058, FM-059, FM-060; ADR-015 (connector lifecycle), ADR-016 (coexistence
  truthfulness), ADR-017 (evidence bundle), ADR-033 (H3 slices and gates), ADR-046 (Run G
  convergence); P14/pillar.md.

## Context

`specs/pillars/01-deduplicated-feature-map.md` marks FM-058, FM-059 and FM-060 as H3/Gated: implementation
requires explicit authorization that no earlier run could grant. Run H therefore ran three read-only gate
tasks after Run G converged, each verifying prerequisites and returning either an authorization or a deferral
record. Because their specs required *no source changes*, the decisions existed only in task output — this
ADR is their durable record.

Prerequisite evidence verified by the gates (all satisfied):

- FM-052/FM-055: outbox atomicity, dispatcher `MAX_ATTEMPTS=5` with backoff and safe-error redaction,
  16-type additive-only `EVENT_CATALOG`, idempotent webhook fan-out (`architecture-runG.md` §8).
- FM-026: local import pipeline; `infrastructure/imports/onbase_panzura_placeholder/` deferred/gated.
- Constitution V.10 / XII.5 and P14 §1-§3: no connector is enabled without explicit authorization,
  named design and an available enterprise environment (Issues #1-#3 of AGENTS.md mean no corporate DB
  and no Azure connectivity on this machine).

## Options

1. **Implement the gated capabilities now** — fastest demo, but violates Constitution V.10/XII.5, P14 Q3
   and ADR-015; would also require enterprise network/credentials that do not exist here.
2. **Silently skip the gates** — leaves no audit trail that the decision was considered; unacceptable for
   a compliance-oriented platform.
3. **Record explicit authorizations and deferrals as a durable ADR**, keeping implementation out of scope
   until a named authorization exists.

## Decision

### H-058 — FM-058 named connectors: DEFERRED (nothing authorized)

- **Authorized named-connector scope: none.** Zero connectors approved for design, implementation,
  enablement or operation.
- `ConnectorKind` values other than `SYNTHETIC` — explicitly `ONBASE` and `PANZURA`, plus any future kind —
  remain Gated per Constitution V.10/XII.5, P14 Q3 and ADR-015.
- `Action.CONNECTOR_NAMED_IMPLEMENT` remains ungranted to every role. No role grant, no credential, no
  network path, no enterprise environment is approved.
- Any future approval must be a separate ADR naming: the exact `ConnectorKind`/`key`/`tenant_id` scope and
  `adapter_ref`; the one-way lifecycle `DRAFT→APPROVED→ENABLED→DRAINING→RETIRED` with `RETIRED` terminal and
  no re-enable; the data classes involved (`Connector`, `ConnectorAuthorization` requiring
  `authorization_rationale`, `CoexistencePolicy`); and drain semantics (`begin_drain` refuses new traffic,
  `retire` only when `in_flight == 0`, otherwise HTTP 409) — per ADR-015 §1/§4.

### H-059 — FM-059 coexistence / migration: AUTHORIZED (limited) + DEFERRED (the dangerous parts)

**Authorized scope:**

- Define coexistence policy: `key`, `source_systems` (closed set), `ownership ∈ {legacy, this_platform}`,
  migration state, `provenance_note`, and `real_time_sync_enabled=False`.
- `record_import_evidence` with batched import-job ids only, advancing `UNMIGRATED→IMPORTED`.
- Operator-driven `set_migration_state` across the closed state set
  `unmigrated|imported|reconciled|divergent|retired_from_source`, with audit + outbox.
- Synthetic labels only.

**Deferred / denied:** `real_time_sync_enabled=True`, `ownership=SHARED`, any named-connector enablement,
any claim of real-time or bidirectional sync, and any corporate/legacy network connection. The absence of
`last_sync_at` / `sync_status` / `outbound_change_log` is design, not a gap (ADR-016). `POST /admin/coexistence`
keeps returning 403 for `real_time_sync_enabled=True`.

### H-060 — FM-060 portable evidence bundles: AUTHORIZED (scoped export, no deferral)

Approved export scope (bundle layout `case.json` + `documents/<id>/metadata.json|content.bin` + `audit.json`):

1. Case contents redacted via `redact_case_data` + schema allowlist; `case_type_version_id` pinned;
   `version_token` and sensitivity carried through.
2. Document `sha256`/`size` read from actual `DocumentStore.get` bytes; classification, sensitivity and
   lock preserved; one manifest entry per file.
3. Full per-case audit timeline (limit 10k) with `hash`/`prev_hash` verbatim and payload redaction — the
   bundle stays replayable off-system.
4. Authorization: `CASE_READ` + `DOCUMENT_READ` re-evaluated live; denied → 403, cross-tenant → 404 (no
   existence oracle); sensitive read requires `CASE_READ_SENSITIVE`; reason required (422); no partial
   bundles; store failure → 503; tenant-scoped throughout.
5. Policy version is the tuple `(BUNDLE_SCHEMA_VERSION="1.0", case_type_version_id,
   schema.sensitive_field_keys + PAYLOAD_SAFE_KEYS)`.
6. Manifest `exported_by`/`exported_at` plus audit `actor_id`/`occurred_at`/`correlation_id`, committed in
   the same transaction as the read.
7. `AuditExported(bundle=True)` with manifest hash, counts and reason; originals are never mutated.
8. Export is permitted under legal hold (hold blocks disposition, not reads); it neither places nor clears
   a hold; held-case disposition still returns `refused_hold`.
9. Redaction is equivalent-by-construction to inline paths and pinned by tests (`ssn` redaction, audit
   `***` allowlist, safe errors only); distinct from T5 `DocBundle` live grants per ADR-042.

**Follow-up (non-blocking):** `AuditExported` carries `reason`/`case_id`/`counts`/`manifest_hash` but no
explicit `policy_version` field; add it as a separate change so the approved tuple is persisted verbatim.

## Consequences

- The platform ships with connector, sync and corporate-network capabilities present only as seams and
  placeholders; nothing dials out (correct given Issues #1-#3).
- Reviewers must treat any PR granting `CONNECTOR_NAMED_IMPLEMENT`, flipping `real_time_sync_enabled`, or
  adding a non-`SYNTHETIC` adapter as requiring a new ADR that supersedes this one.
- The H3 rows in `01-deduplicated-feature-map.md` can be checked off as *decided* (authorized/deferred),
  not as *implemented* — except FM-060, whose scoped export implementation already exists and is now
  explicitly approved.
- One small code follow-up is open: persist `policy_version` on `AuditExported`.

## Azure-seam note

No Azure or corporate call is made anywhere in this decision. When a real environment exists: the connector
seam (`domain/connector.py` + `app/infrastructure/connectors/`) receives the first named adapter; the
coexistence repo placeholder is replaced by a SQL adapter; evidence-bundle storage continues to go through the
`DocumentStore` port (`local_document_store` → `AzureBlobDocumentStore`). Each swap still requires its own
named authorization ADR before it may be enabled.
