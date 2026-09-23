# ADR-002: Integration seams — local adapters now, Azure adapters later

- Status: Accepted (2026-09-01)
- Scope: AGENTS.md §12, `docs/integrations/seams.md`,
  `backend/app/config.py`, `backend/app/infrastructure/{secrets,telemetry}/`,
  `backend/app/deps.py`.
- Supersedes: nothing.
- Related: AGENTS.md §3 Issue #2 (no Azure connections possible locally),
  §12 (seam inventory), §14 (Definition of Done includes health adapter
  visibility), §18 (observability contract). plan.md:42, plan.md:154-180.

## Context

The first slice targets local PostgreSQL + filesystem + in-process bus
today and Azure SQL + Blob + Service Bus + Entra + Key Vault in
production. Constitution §VI requires us to *architect for Azure today*
without depending on Azure at runtime. AGENTS.md §3 Issue #2 makes the
constraint operational: there is no Azure connectivity on this host, so
every integration point must declare a local adapter plus a reserved
Azure adapter slot that does nothing until the seam is swapped.

Concurrently, AGENTS.md §18 requires every log line to include
`correlation_id`, `tenant_id`, and `actor_id`, plus a redaction list
covering `ssn`, `phi`, `secret`, `password`. The walking-skeleton
correlation middleware already bound `correlation_id` + `method` +
`path`, but the actor/tenant binding lived only in the request handler
and the redaction layer did not exist.

This ADR closes both gaps: it ships the secrets seam end-to-end (local
adapter + Protocol + DI + env-var contract) and it lands the telemetry
seam's redaction + actor/tenant binding so every log line meets the
observability contract.

## Options considered

1. **Inline adapters in `app/deps.py`**
   - Pros: shortest path, no new module.
   - Cons: bloats `deps.py` past the soft limit and bleeds infrastructure
     imports into the dependency module. Violates the layer rule's
     *spirit* even though the rule does not forbid it today.
2. **Per-seam sub-package + Protocol in `domain/ports.py`**
   - Pros: keeps each seam swappable from day one; tests can target the
     Protocol without booting the FastAPI app; Azure adapter slots live
     next to the local adapter.
   - Cons: more files. The Protocol can drift from the adapter if not
     pinned by a test.
3. **Single mega-module `infrastructure/adapters.py`**
   - Pros: one import statement.
   - Cons: high file size (>1000-line risk, AGENTS.md R2), and
     cross-cutting concerns mix into a single module. Hard to test in
     isolation.

## Decision

Adopt **option 2** with three concrete rules:

1. Every seam gets a sub-package under
   `backend/app/infrastructure/<seam>/`. The first slice ships `secrets/`
   (this ADR) and `telemetry/` (this ADR); the other six seams follow
   the same pattern when their adapters land.
2. The Protocol lives in `app/domain/ports.py` (or
   `app/application/<feature>/ports.py` for application-scoped seams)
   and is annotated with `# INTEGRATION_SEAM: <azure_target>`. The local
   adapter implements it structurally (no inheritance).
3. A new `docs/integrations/seams.md` is the inventory of record. It
   lists every seam, the local/Azure adapter, the wiring location
   (`app/deps.py`), and a `status` column so reviewers can see at a
   glance which seams are live.

Concretely, this ADR adds:

- `Settings.secret_provider` (`local_env` | `azure_key_vault`,
  default `local_env`) and `Settings.secrets_local_store_path`
  (default `./.secrets.json`) — `app/config.py`.
- `SecretProvider` Protocol enriched with `set_secret`
  (`app/domain/ports.py`).
- `LocalSecretProvider` adapter
  (`backend/app/infrastructure/secrets/local_secret_provider.py`)
  backed by `APP_SECRET_<NAME>` env vars + a JSON file store. Atomic
  write via temp-file + replace.
- `configure_structlog` (`backend/app/infrastructure/telemetry/structured_logging.py`)
  with the redaction list (`ssn`/`phi`/`secret`/`password`/`authorization`/
  `x-dev-user`/`x-dev-tenant`/`x-api-key`/`token`).
- `correlation` contextvars module
  (`backend/app/infrastructure/telemetry/correlation.py`) that binds
  `correlation_id`, `tenant_id`, `actor_id`, `method`, `path` into
  structlog contextvars.
- `app/deps.py:get_secret_provider` (FastAPI dependency) and the
  `get_current_user` actor/tenant binding.
- `app/main.py` logs the active adapter set at startup and uses
  `configure_structlog`.
- `backend/tests/foundation/test_telemetry.py` — covers correlation
  binding, redaction (key-based for `ssn`/`password`/`token`/etc.,
  nested-walk), end-to-end log redaction, and the local SecretProvider
  env+file path.

## Consequences

Positive:

- Every seam is replaceable from a single location (`app/deps.py`); the
  Azure swap is one config-flag change per seam.
- Operators can confirm which adapter is active by reading
  `/api/v1/health` (FR-034) or the startup log — both now enumerate the
  adapter set.
- The redaction list is centralised; the same processor protects both
  log emission and audit-payload filtering (Constitution V).

Negative:

- A new module (`secrets/`, `telemetry/`) increases the on-disk surface.
  Mitigated by the AGENTS.md §15 <1k-line file limit (each new file is
  well under 200 lines) and by `ruff check`/`mypy --strict` in CI.
- `azure_key_vault` is selected at startup with a fail-closed error.
  This is intentional (AGENTS.md §3 Issue #2 — no Azure connections
  locally) but means a misconfigured `SECRET_PROVIDER=azure_key_vault`
  prevents the app from booting. The error message names the cause.

Operational:

- `app/startup` log line now carries the `adapters` object so ops can
  tail logs to verify adapter selection in dev/test before deploy.
- `LocalSecretProvider.set_secret` writes through to the JSON file; the
  file is gitignored via the standard `.gitignore`. A future operator
  workflow can rotate secrets by deleting the file and re-running
  the seeder.

## Seam note

- Seam #5 (Secrets) — local adapter shipped; Azure Key Vault adapter
  reserved at `infrastructure/secrets/azure_key_vault.py` (not present
  yet). The Protocol is enriched with `set_secret` so the local adapter
  can seed the store; the Azure adapter should override `set_secret` to
  raise `NotImplementedError` because secret rotation belongs in the
  operator workflow, not the app.
- Seam #6 (Telemetry) — local structlog adapter shipped with redaction.
  The Azure adapter is an OTLP exporter added under
  `infrastructure/telemetry/otlp.py`; selection switches on
  `Settings.telemetry == "otlp"`. Not implemented in this slice because
  no Azure Monitor endpoint is reachable on this host.
- The seam inventory lives at `docs/integrations/seams.md` (new file);
  review this file alongside any new Protocol in `app/domain/ports.py`.

## Verification

- `uv run pytest backend/tests/foundation/test_telemetry.py -q` — green.
- `uv run ruff check backend` and `uv run mypy --strict backend/app/domain backend/app/application`
  — green.
- `uv run lint-imports verify` — green (no new layer violations; the
  secrets/telemetry modules import only stdlib + structlog).
- `POST /api/v1/health` (when the app boots) returns
  `adapters.secret_provider: "local_env"` and the startup log line
  carries the same `adapters` object.