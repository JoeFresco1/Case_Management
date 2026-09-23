# ADR-045 — System configuration, diagnostics, and storage-admin surface (T7)

**Status**: Accepted · **Date**: 2026-09-11 · **Slice**: `002-leftovers` T7

## Context

`specs/002-leftovers/spec.md:177` requires diagnostics + system utilities
(per-module logging, cache/service utilities behind a `System
Configuration` right; storage volume/cache abstraction + at-rest
encryption toggle as adapter config, never domain logic). No such surface
existed: log levels were process-global (`Settings.log_level`), cache
utilities did not exist, and storage/encryption choices were implicit in
`Settings.documents_path` plus a seam note in `structured_logging.py`.

## Options

1. **Persist settings in a new SQL table** with full audit/outbox wiring.
2. **Application-owned versioned settings** (in-memory store behind a
   Protocol, SQL later) + infrastructure-only adapters + a thin
   `ADMIN_TENANT_CONFIG` router.
3. **Bake choices into `Settings` env vars only** (no admin API).

## Decision

Option 2, in exactly three source files plus tests:

- `app/application/configuration/system_settings.py` — tenant-scoped,
  versioned (optimistic concurrency, stale → `Conflict`/409), audited
  (per-version history + redacted `SystemSettingUpdated` payload),
  validated per category, secret-redacted on every outward surface.
- `app/infrastructure/system/local_system_adapters.py` — `LocalStorageVolume`
  (serves `local-fs`, fail-closed on `azure-blob`), `LocalEncryptionAtRest`
  (plaintext-local, fail-closed when enabled without KeyVault),
  `LocalDiagnosticsService` (TTL cache stats + clear, service status).
  No Azure SDK imported or required.
- `app/interfaces/http/routers/admin_system.py` — `GET/PUT
  /admin/system/settings[/{key}]`, `GET /diagnostics`, `POST
  /cache/clear`, `GET /storage`, all behind `ADMIN_TENANT_CONFIG`.
  Infra-free (duck-typed `app.state`, settings-derived fallbacks) per the
  import-linter contract. Composition-root wiring in `app/main.py` is
  minimal glue (state slots + `include_router`).

Option 1 was rejected as over-build for this slice (SQL migration +
outbox fan-out for data that is read-rarely/written-rarely); the Protocol
keeps the SQL upgrade path open. Option 3 was rejected because env-only
config is not versioned, not audited, and not tenant-scoped.

## Consequences

- Admins get impact-safe edits: concurrent overwrites are 409, every
  version keeps actor/summary, secrets never render (`[REDACTED]`).
- Storage/encryption posture is explicit (`declared` vs `actual` in
  `GET /storage`); declaring `azure-blob`/enabled-encryption without the
  seam fails closed instead of silently degrading.
- 31 new tests (use-case + router, two-tenant, Azure-free) green;
  `ruff + format + mypy --strict + import-linter` clean.

## Seam note

- Seam 2 (Document store): `LocalStorageVolume` serves `local-fs`;
  `AzureBlobDocumentStore` remains the production seam.
- Seam 5 (Secrets/encryption): `LocalEncryptionAtRest` is plaintext-local;
  `AzureKeyVault` field encryption remains the production seam.
- Seam 6 (Telemetry): `elk`/`splunk` sinks are declared config only;
  no forwarding connection is opened in this slice.
