# Integration Seams Inventory

> Source of truth for every place where a local adapter will eventually be
> swapped for an Azure/corporate adapter. Mirrors the seam table in
> `AGENTS.md §12` and `specs/001-case-management-platform/architecture.md`.
> Add an entry when a new Protocol lands in `app/domain/ports.py` or
> `app/application/`; remove an entry only when the seam is retired.

## Status legend

- **Local adapter implemented** — wired in `app/deps.py` and used in dev/test.
- **Local adapter implemented (placeholder for Azure)** — same as above, plus a
  `azure_*` adapter slot reserved in `app/infrastructure/` for the production
  swap.
- **Azure adapter pending** — seam is declared but not yet implemented.
- **Defer until Phase 3+** — explicitly out of scope for this slice.

## The 8 Seams

| # | Seam | Port | Local adapter | Local status | Azure adapter | Azure status | Where it is wired |
|---|---|---|---|---|---|---|---|
| 1 | Relational DB | `CaseRepository`, `CaseTypeRepository`, `CaseTypeVersionRepository`, `TenantRepository`, `AuditEventStore`, `OutboxWriter`, `IdempotencyStore` (`app/domain/ports.py`) | `infrastructure/persistence/repositories/*` (SQLAlchemy 2 async + asyncpg) | Local adapter implemented | `infrastructure/persistence/azure_sql_placeholder/` (aioodbc + Azure SQL) | Azure adapter pending (placeholder) | `app/db.py:39`, `app/deps.py:get_db` |
| 2 | Document store | `DocumentStore` (`app/domain/ports.py`) | `infrastructure/storage/local_document_store.py` | Local adapter implemented | `infrastructure/storage/azure_blob_placeholder/` (`AzureBlobDocumentStore`) | Azure adapter pending (placeholder) | `config.documents_path` |
| 3 | Messaging | `MessageBus` (`app/domain/ports.py`) | `infrastructure/messaging/local_message_bus.py` + durable outbox + `outbox_dispatcher.py` (event catalog `app/domain/events.py:EVENT_CATALOG`) | Local adapter implemented | `infrastructure/messaging/azure_servicebus_placeholder/` (`AzureServiceBus` publisher) | Azure adapter pending (placeholder) | `app/main.py:_startup` |
| 4 | Identity | `IdentityProvider` (`app/deps.py`) | `LocalHeaderIdentityProvider` (`app/deps.py`) — reads `X-Dev-User`/`X-Dev-Tenant` only when `APP_ENV=local` | Local adapter implemented (placeholder for Azure) | `infrastructure/identity/entra_placeholder/` (`EntraIdentityProvider` — Microsoft Entra ID via MSAL) | Azure adapter pending (placeholder) | `app/deps.py:get_current_user` |
| 5 | Secrets | `SecretProvider` (`app/domain/ports.py`) | `LocalSecretProvider` (`infrastructure/secrets/local_secret_provider.py`) — env `APP_SECRET_<NAME>` then JSON file | Local adapter implemented | `infrastructure/secrets/azure_keyvault_placeholder/` (`AzureKeyVaultSecretProvider`) | Azure adapter pending (placeholder) | `app/deps.py:get_secret_provider` (selects via `Settings.secret_provider`) |
| 6 | Telemetry | `Telemetry` (`app/domain/ports.py`) + structlog contextvars | `infrastructure/telemetry/structured_logging.py` (JSON via `structlog.PrintLoggerFactory`) + `correlation.py` | Local adapter implemented | `infrastructure/telemetry/azure_monitor_placeholder/` (OTLP exporter → Azure Monitor / Application Insights) | Azure adapter pending (placeholder) | `app/main.py:configure_structlog` |
| 7 | AI assistance | `AIAdvisor` (`app/application/ai/boundary.py`) — single verb `recommend(...)` returning `AIRecommendation` with `confidence`, `source_refs`, `model_id`, `warnings` | `NoOpAIAdvisor` (`infrastructure/ai/noop_stub.py`) — zero-confidence empty recommendation, `model_id="noop"`, deterministic, no I/O | Local adapter implemented | `infrastructure/ai/azure_openai_placeholder/` (`AzureOpenAIAdvisor` placeholder) | Azure adapter pending (placeholder) | §4.R11; ADR-012; FM-050 |
| 8 | Enterprise imports | `ImportJobRepository`, `ImportMappingRepository`, `ChannelRepository`, `QuarantineRepository`, `CapturedEmailRepository`, `CapturedWebhookRepository` (`app/domain/ports.py`) | Import pipeline (`app/application/imports/run_import.py` + `source_ids.py`), channel/quarantine governance (`manage_channels.py`), capture seams (`application/capture/*`), SQL adapters (`infrastructure/persistence/repositories/*`), migration `0030_capture_quarantine_webhook`, synthetic operator UI (`frontend/src/imports/`) | Local adapter implemented | `infrastructure/imports/onbase_panzura_placeholder/` (OnBase + Panzura connectors) | Defer until Phase 3+ (Gated, placeholder reserved) | Run D (`specs/002-leftovers/architecture-runD.md`); FM-026/027 implemented, FM-028/029/030 fail-closed seams |

## Active seam env-var contract

| Env var | Default | Used by seam | Notes |
|---|---|---|---|
| `APP_ENV` | `local` | all seams | `local` allows dev-header identity; `test`/`shared`/`prod` fail closed on it |
| `DATABASE_URL` | `postgresql+asyncpg://cases:cases@localhost:5432/cases` | 1 (Relational DB) | sqlite profile via `sqlite+aiosqlite:///./.pytest.db` for tests |
| `AZURE_SQL_CONNECTION_STRING` | _unset_ | 1 (Relational DB) | Set only when Azure SQL adapter is wired; the placeholder reads it for logging |
| `DOCUMENTS_PATH` | `./documents` | 2 (Document store) | created at startup if missing |
| `AZURE_BLOB_ACCOUNT_URL` | _unset_ | 2 (Document store) | `https://<account>.blob.core.windows.net`; placeholder reads it for logging |
| `AZURE_BLOB_CONTAINER` | `case-documents` | 2 (Document store) | blob container name |
| `AZURE_SERVICE_BUS_FQDN` | _unset_ | 3 (Messaging) | `https://<namespace>.servicebus.windows.net` |
| `AZURE_SERVICE_BUS_TOPIC` | `case-events` | 3 (Messaging) | publisher topic name |
| `AZURE_ENTRA_TENANT_ID` | _unset_ | 4 (Identity) | Entra tenant id; placeholder refuses to start without it in prod posture |
| `AZURE_ENTRA_CLIENT_ID` | _unset_ | 4 (Identity) | Entra app registration client id |
| `AZURE_ENTRA_AUDIENCE` | _unset_ | 4 (Identity) | expected `aud` claim for issued tokens |
| `SECRET_PROVIDER` | `local_env` | 5 (Secrets) | `azure_key_vault` raises at startup (fail-closed) |
| `SECRETS_LOCAL_STORE_PATH` | `./.secrets.json` | 5 (Secrets) | parent dir created at startup; the file is created lazily on first write |
| `AZURE_KEY_VAULT_URL` | _unset_ | 5 (Secrets) | `https://<vault>.vault.azure.net/` |
| `LOG_LEVEL` | `INFO` | 6 (Telemetry) | passed to structlog + stdlib logging |
| `TELEMETRY` | `structured` | 6 (Telemetry) | `structured` = local structlog JSON; `otlp` is reserved for the Azure Monitor adapter |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _unset_ | 6 (Telemetry) | Azure Monitor / App Insights ingestion endpoint (placeholder only) |
| `AZURE_OPENAI_ENDPOINT` | _unset_ | 7 (AI assistance) | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | _unset_ | 7 (AI assistance) | model deployment name (e.g. `gpt-4o`) |
| `AZURE_OPENAI_API_VERSION` | `2024-08-01-preview` | 7 (AI assistance) | Azure OpenAI API version |
| `TENANT_LISTENER_ENABLED` | `True` | 1 (Relational DB) | defense-in-depth only; never the sole tenant isolation mechanism |
| `SLA_SCANNER_ENABLED` | `True` | 1 (Relational DB) / scheduler | Enables the local in-process SLA scan loop; disable for one-shot/test app lifecycles |
| `SLA_SCAN_INTERVAL_SECONDS` | `60` | scheduler | Cadence for the local scanner; production workers reuse the scanner contract |

## Wiring rule (every seam)

1. **Define** the Protocol in `app/domain/ports.py` (or
   `app/application/<feature>/ports.py` if it is application-scoped, not
   domain-scoped).
2. **Implement** the local adapter in `app/infrastructure/<seam>/`.
3. **Reserve** the Azure adapter placeholder in
   `app/infrastructure/<azure_target>_placeholder/` with an `INTEGRATION_SEAM`
   marker and a README listing the env vars it would need.
4. **Wire** the adapter via `app/config.py` (selector field) + `app/deps.py`
   (dependency function) + `app/main.py` (startup log).
5. **Annotate** the seam location with `# INTEGRATION_SEAM: <azure_target> —
   adapter contract, replaced when corp env available`.
6. **Document** the seam here with the local/ azure adapter paths and the
   current status (do not mark "implemented" until both the local adapter
   works AND a unit test covers its behavior).
7. **Verify** the seam appears in `/api/v1/health` (FR-034) — every adapter
   must log its selection at startup.

## Audit (every seam change)

- Add or update the seam row above when a new port lands.
- Add an ADR (`docs/decisions/ADR-NNN-<slug>.md`) summarizing the
  Context / Options / Decision / Consequences / **Seam note** (which seam,
  which adapter, which env vars).
- Update the seams section of `specs/001-case-management-platform/architecture.md`
  if the slice's Mermaid diagram or seam table diverges from this file.

## Placeholder directory convention

Every `app/infrastructure/<azure_target>_placeholder/` directory:

1. Contains a `README.md` that lists the seam it implements, the env vars
   it will need, the Protocol it must satisfy, and a stub-class signature
   (`class AzureXxxAdapter: ...`) showing what the production adapter will
   look like.
2. Carries a `# INTEGRATION_SEAM: <AzureTarget> — adapter contract,
   replaced when corp env available` marker at the top of `README.md`.
3. Does NOT import any Azure SDK at import time (intentional — the
   placeholder must not drag the dependency into the synthetic environment).
4. Is referenced by this document so a future reader can locate the seam.
