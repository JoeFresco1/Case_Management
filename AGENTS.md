# AGENTS.md — Case Management Platform

> **For autonomous coding agents (lightweight-dag orchestrated).** This is the execution contract. Read `specs/pillars/00-product-roadmap.md`, `specs/pillars/01-deduplicated-feature-map.md`, `specs/001-case-management-platform/first-slice/spec.md`, `specs/001-case-management-platform/first-slice/plan.md`, and `.specify/memory/constitution.md` before any code change. Constitution is highest authority.

## 1. Objective

Build a secure, case-centric, configurable, versioned, auditable, API-enabled enterprise workflow platform that runs **locally with synthetic data today**, targets **Azure in production tomorrow**, and can incrementally replace legacy capabilities without cloning or depending on legacy systems.

First slice : `001-case-management-platform` (Phases 0-3). Pillars in scope: P01, P02 (partial), P03 (partial), P05, P09, P10 (partial), P11 (foundation), P13 (foundation). All other pillars deferred — do not build them early.

## 2. Operating Mode: Autonomous via Orchestration

This repo is built **autonomously with minimal human intervention**. Human is on personal computer; no corporate network is available.

- **Default to the lightweight `dag` CLI (DAG Orchestrator; source `C:\Users\Fresc\lightweight-dag`, globally linked).** Decompose any feature slice from `specs/001-.../tasks.md` or `specs/pillars/01-deduplicated-feature-map.md` into a DAG before coding. One Run = one DAG (`dag init --objective "..."`). Create tasks with `dag add --title T --spec S [--deps a,b]` where `--spec` is self-contained (inputs/outputs/acceptance) and `--deps` is a comma list. Verify with `dag list` / `dag status` / `dag show --id <task_id>`.
- **Parallelism:** `dag settings --concurrency 4`. Tasks with satisfied `--deps` run concurrently up to the cap. Optional per-task worktrees (`dag settings --worktree task`) land verified work on `dag/<runId>` and leave your current checkout untouched.
- **Gates:** Add approval gates with `dag gate --id <task_id> --question "..."` at design-approval and pre-publish validation points; resolve them with `dag approve --id <task_id>` or `dag reject --id <task_id>` (`dag settings --gates skip` deliberately converges without waiting). Do not block indefinitely — record decision and proceed.
- **Execution belongs to the human (or explicit request):** interactive via `dag serve --open` -> http://localhost:8787 -> press Run; headless via `dag run --concurrency N`. Agents **build the graph only** (`init`/`add`/`edit`/`gate`/`settings`) unless explicitly asked to execute. Run state lives in `dag.run.json` + `dag.run.d/`.
- **Never hand-edit run state or delete `dag.run.json` to "reset".** To redraw, archive the active run and start fresh: `dag new-run --objective "..."`.
- **Default harness:** `opencode` (`dag set --harness opencode`, presets via `dag harness list`). Override per-task with `dag set --only <id> ...` or in the viewer if needed.

## 3. Hard Constraints (Issues)

**Issue #1 — Existing DB connections not possible. Issue #2 — Azure connections not possible. Issue #3 — No Docker Desktop; WSL registry issue makes WSL-based runs unreliable.**

Consequence on this machine:
- Use **PostgreSQL 16 via `docker-compose.yml`** only (see Rule 1). `DATABASE_URL=postgresql+asyncpg://cases:cases@localhost:5432/cases`. No tunnel to corporate DB.
- All Azure services are **contracts only**. Implement against Protocols/ports — do not attempt live Azure calls; do not require `az login` or Entra tenant.
- **Docker Desktop is not available and WSL is flaky** due to a registry issue on the host. `docker` CLI tooling *does* work via CLI (`docker`, `docker compose`), but any workflow that assumes Docker Desktop GUI or a healthy WSL backend will fail. Agents must: (a) verify via `docker info` / `docker compose version` before assuming postgres is up; (b) treat `docker compose up -d` failures as expected and fall back gracefully; (c) never block a task solely because the container won't start — use the sqlite test profile or in-memory fakes where the task allows it.
- Where an Azure/external integration would be called, leave a clearly marked seam (see §12) and document it in the spec folder. Do NOT stub with conditional `if local else azure` inside `domain`/`application`. Environment differences belong in `app/infrastructure/*` adapters and `app/config.py` wiring via DI (`app/deps.py`, `app/main.py:56`).

## 4. The 12 Rules (Enforced)

### R1 — Local Postgres only
- `docker compose up -d` -> `cmp-postgres` on `5432` **via CLI only** — no Docker Desktop, no WSL assumption. WSL registry is corrupted, so container startup may fail even though `docker` CLI is present. Use SQLAlchemy generic types only: `Uuid`, `JSON`, `DateTime(timezone=True)`, `Text`, `String`, `Integer`, `Boolean`. No `JSONB`, `GIN`, `ILIKE`, `tsvector`, `gen_random_uuid()`, array/range types. Search starts with portable `LIKE`. See `specs/001-case-management-platform/first-slice/plan.md:289`.
- Alembic migrations must be portable SQL (`plan.md:301`). Add portability comment if raw dialect SQL unavoidable.
- **WSL/Docker Desktop fallback (mandatory):** Every task that needs Postgres must verify first (`docker info`, `docker compose ps`) and handle failure. For tests/CI use the sqlite profile which never needs Docker: `APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q` (`backend/pyproject.toml:71`, `AGENTS.md:121`). Domain/application unit tests should prefer in-memory fakes over a live container. Do not fail a DAG node solely because `docker compose up -d` errored — log the WSL limitation, run sqlite verification, and continue.

### R2 — Small files (<1k lines), refactor regularly
- Hard limit: **no source file >1000 lines**. Soft limit: refactor at **>600 lines** or cyclomatic complexity >10. Split by responsibility, not by layer copy-paste. Run `ruff check` and `import-linter verify` after each refactor. Commits that introduce >1k files fail review — agent must split first.

### R3 — Be Skeptical
- Challenge every generation. Before committing: (a) walk the call path via `codegraph explore "<symbol>"` — grep misses dynamic dispatch; (b) verify type correctness (`mypy --strict app/domain app/application`); (c) verify layer violations (`import-linter`); (d) run relevant tests. If spec/plan/code conflict, **spec + constitution win**. Open questions belong in `specs/pillars/PXX-*/pillar.md` — do not guess.
- Prefer deleting clever code over defending it (Constitution XV). If an abstraction lacks a live use case, remove it.

### R4 — Excessive Comments & Readability
- Every public function/class/protocol needs a docstring: purpose, args, returns, failure mode. Every non-trivial guard, hash, or transaction boundary needs an inline comment explaining *why* (not *what*). Example: audit hash `audit.py:26`, tenant isolation `ports.py:24`, outbox atomicity `plan.md:206`. Verbose is correct here.

### R5 — Organized, Sortable Tests with Clear Names
- Location: `backend/tests/`. Layout mirrors source: `tests/domain/test_case.py:11`, `tests/application/cases/test_create_case.py:11`, `tests/interfaces/http/test_cases_router.py:11`.
- Naming: `test_<unit>_<scenario>__<expectation>` e.g. `test_create_case__rejects_missing_required_field`. Tests must be sortable (`pytest --collect-only` alphabetical) and filterable (`-k`).
- Structure: `// given / // when / // then` with Arrange-Act-Assert. Every use case tests: success, validation failure, auth denial, idempotent replay, stale concurrency token, dependency failure (storage unavailable).
- Gate: `pytest -q` with `pytest-cov >=80%` on `app/domain`+`app/application` (see `backend/pyproject.toml:71`). No slice is done unless its P1/P2 user stories have automated acceptance tests in `specs/001-.../tasks.md`.

### R6 — Readable Code
- Strict typing, no `Any` outside ports. `ruff` line length 100 (`pyproject.toml:43`), `target-version=py312`. Frontend: strict TS, `eslint`+`prettier`, `tsc -b --noEmit`. Prefer boring over clever. Name things with business terminology, not legacy terms (Constitution I).

### R7 — API-First, Good API Documentation
- URL prefix `/api/v1/...` (see `plan.md:268`). Every endpoint needs Pydantic request/response schemas, OpenAPI tags, pagination (`Page[T]` default 50 max 200 `plan.md:284`), idempotency on mutations (`Idempotency-Key` header `interfaces/http/idempotency.py`), optimistic concurrency via `If-Match: <version_token>`, and structured `ErrorResponse{code,message,details,correlation_id}` (`interfaces/http/errors.py`). Never leak stack traces/SQL/secrets (Constitution V).
- OpenAPI is the contract: `app/main.py:60` mounts `health` + `cases` routers. Frontend types generated via `openapi-typescript` from `/openapi.json` — never hand-type API shapes. Document in `specs/NNN-*/` and in OpenAPI descriptions.

### R8 — Decisions Documented
- Every material decision → `docs/decisions/ADR-NNN-title.md` (or `specs/NNN-*/decisions.md` for slice-local). Template: Context / Options / Decision / Consequences / Azure-seam note. Link from `specs/pillars/00-product-roadmap.md §7` or the slice's `plan.md`. No silent choices.

### R9 — Architecture with Mermaid After Each Feature
- When a feature slice converges, record architecture in its spec folder: `specs/NNN-short-name/architecture.md` containing a **Mermaid JS** diagram (flowchart / sequence / ER) + module map + port list. Copy the template from `docs/TECH_SPEC.md` if exists. CI checks that `architecture.md` exists before marking slice done.

### R10 — Scalability, Ease of Integration & Maintenance
- Modular monolith with explicit boundaries (`backend/app/domain|application|infrastructure|interfaces` `plan.md:50`). Domain is pure (no FastAPI/SQLAlchemy imports). Application depends on domain only (imports as `Protocol` ports `domain/ports.py:24`). Infrastructure implements ports. Interfaces depend on application only. Enforced by `import-linter` (`pyproject.toml:84`).
- Multi-tenant from day one: explicit `tenant_id` on every repository method (`ports.py:26`) + `WHERE tenant_id = :tenant_id` in every query. `before_compile` tenant listener (`config.TENANT_LISTENER_ENABLED`) is defense-in-depth only — never sole mechanism.
- Idiomatic split: push cross-cutting concerns (pagination, idempotency, correlation, telemetry) into middleware/deps, not into domain. Keep config declarative JSON (workflow/rule schemas), validated at publish time (`plan.md:234`).

### R11 — Placeholder for AI
- AI is **bounded assistance only** (Constitution XIV / P11-US12). Domain must work without LLM. Where AI would add value, leave a clean seam — do not wire an LLM call into workflow transitions, authorization, or disposition.
- Pattern: add a `app/application/ai/<capability>.py` Protocol with a no-op/local stub and an `azure_ai` adapter placeholder behind `app/infrastructure/ai/`. Document seam in spec per §12. Log recommendation + confidence + source context separately from deterministic decision; human authorization remains authoritative. Example seams: extraction (P05), search ranking (P06), workflow-authoring suggestions (P10), insights (P12).

### R12 — Security First
- Fail-closed. Deny by default (`application/authorization.py:policy.evaluate`). Server-side enforcement only (Constitution V) — UI visibility never = authorization.
- Local dev identity via `X-Dev-User`/`X-Dev-Tenant` headers only when `APP_ENV=local` (`app/deps.py:98`). In `shared`/`prod` these headers are rejected with 401. `SYNTHETIC_ENVIRONMENT=1` marker must be visible in logs and UI (`app/main.py:95`).
- No PHI/secrets/production identifiers in fixtures/logs/audit payloads. Sensitive values referenced by id, not content (`plan.md:259`). Redaction list in `infrastructure/telemetry/structured_logging.py`.
- Secrets via `SecretProvider` (env/local store -> Azure Key Vault). Never embed in source, workflow JSON, or logs.
- Audit: append-only `audit_events` with chained SHA-256 (`domain/audit.py:26`), at four layers: DB trigger rejects UPDATE/DELETE, ORM listener raises, application has no mutation path, migrations use compensating `AuditCorrectionRecorded` events (`plan.md:252`). Align with FM-003/FM-043.
- Every protected path must be policy-evaluated and tenant-scoped (FM-002). Include indirect persistence/listener paths in review.

## 5. Stack & Layout

```
backend/ app/ domain/ case.py:1 case_type.py:1 audit.py:1 ports.py:1
            application/ cases/ workflow/ tasks/ documents/ audit/ configuration/
            infrastructure/ persistence/models.py storage/local_document_store.py
                            messaging/ identity/ secrets/ clock/ telemetry/
            interfaces/http/ routers/ schemas/ errors.py idempotency.py
            main.py:56 config.py:18 db.py:12 deps.py:92
       tests/ alembic/ pyproject.toml README.md
frontend/ src/ api/ cases/ tasks/ documents/ audit/ workflow/ shell/ main.tsx  vite.config.ts
docker-compose.yml  .env.example  dag.run.json  dag.run.d/  specs/
```

- Backend: Python 3.12 + FastAPI + SQLAlchemy 2 async + Pydantic v2 + Alembic + structlog + typer + uuid7
- Frontend: React 18 + Vite + TS strict + TanStack Query + React Router + openapi-typescript + vitest
- Tests: pytest + pytest-asyncio, vitest + Testing Library; E2E Playwright deferred
- Toolchain pinned: `.python-version` 3.12, `.nvmrc` 20

## 6. Source of Truth & Pillars

- Pillars: `specs/pillars/P01-.../pillar.md` (14 pillars, long-lived capabilities)
- Roadmap + cross-cut dependencies: `specs/pillars/00-product-roadmap.md:15` and `:70`
- Deduplicated feature catalog + dependency spine + pull order: `specs/pillars/01-deduplicated-feature-map.md:36` — use this to select the next Spec Kit lifecycle. Do not open duplicate specs for H0 rows already in `001`.
- Active slice: `specs/001-case-management-platform/first-slice/{spec.md,plan.md,tasks.md}` — vertical `create -> route -> work -> document -> decision -> close -> timeline` (`tasks.md:9`)
- Constitution: `.specify/memory/constitution.md` — prevails over all other docs.

## 7. Local Development Contract (must keep working)

```bash
docker compose up -d                 # postgres 16 on :5432 — CLI only, no Docker Desktop
uv sync --extra dev
uv run alembic upgrade head
uv run python -m app.cli seed        # synthetic tenant + users + CaseType + demo case
uv run uvicorn app.main:app --reload # :8000, logs "SYNTHETIC ENVIRONMENT"
pnpm --prefix frontend dev            # :5173 proxies /api -> :8000
```

> **Host limitation:** WSL registry corruption makes `docker compose up -d` flaky. CLI `docker`/`docker compose` is available, but Docker Desktop GUI is not. All tasks must probe (`docker info`, `docker compose ps`) before assuming postgres is up and must not fail solely on container-start errors.

Smoke (must pass from clean checkout in <10 min — sqlite path avoids Docker entirely):
```bash
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q
# postgres path (when WSL cooperates):
docker compose up -d; docker compose ps
curl -H "X-Dev-User: <intake-id>" -H "X-Dev-Tenant: <tenant-id>" -H "Idempotency-Key: <uuid>" \
     -H "Content-Type: application/json" -d '{"case_type_key":"ServiceRequest","data":{...}}' \
     http://localhost:8000/api/v1/cases | jq .
```

## 8. How Autonomous Agents Should Work

1. **Select feature** from `01-deduplicated-feature-map.md` in dependency order (`:127` spine, `:158` pull order). Do not pull >2 H1 features concurrently. Respect pillar cross-cuts (`00-product-roadmap.md:70`).
2. **Create a Run** (`dag init --objective "FM-00X: <outcome>"`) and **decompose** the `tasks.md` phase into 4-12 tasks (`dag add --title ... --spec ... --deps ...`). Follow spec-writing rules: inputs/outputs/acceptance, imperative, no "as appropriate".
3. **Build in vertical order:** walking skeleton first (`tasks.md:31` T001-T020) -> harden foundation -> domain -> application -> infrastructure -> interfaces -> frontend seed/demo. Each task creates/modifies a small batch of files and includes its tests.
4. **Be skeptical (§4.R3).** After each task batch: `ruff check`, `mypy --strict app/domain app/application`, `import-linter verify`, `pytest -q`, `pnpm typecheck`/`build`. Fix before marking task done.
5. **Mark seams** (§12) where Azure/corporate integration would plug in; do not block on them.
6. **Record decisions** (§4.R8) and after slice convergence write `specs/NNN-*/architecture.md` with Mermaid (§4.R9).
7. **Gate on Done:** Constitution `Definition of Done` + `tasks.md:169` AC-001..AC-010. All must pass before merge.

## 9. Quality Gates (must pass before PR / task-done)

- `ruff check + ruff format --check` clean (`pyproject.toml:44`)
- `mypy --strict app/domain app/application` clean (no per-file ignores added to dodge strict)
- `lint-imports` / `import-linter verify` clean (layer rules `pyproject.toml:88`)
- `pytest -q --cov` >=80% on `domain`+`application` (`pyproject.toml:82`)
- `pnpm typecheck && pnpm build` clean
- No file >1000 lines; no function >50 lines without justification
- Every new API has OpenAPI entry + error model + idempotency/concurrency as required
- Tenant-isolation test with 2 tenants for every repository/query
- Synthetic-data check: `grep -R example.test` passes, no PHI patterns in fixtures

## 10. API-First Requirements

- Version prefix `/api/v1`, correlation via `X-Correlation-Id` (`main.py:39`, `deps.py:38`), idempotency via `Idempotency-Key`, concurrency via `If-Match`.
- List endpoints paginate; health distinguishes local vs azure adapters (`routers/health.py` FR-034).
- Frontend never hand-types: `openapi-typescript` from `http://localhost:8000/openapi.json`.

## 11. Code Conventions

- Comments: every module has a top-level docstring with file purpose and seam notes. Every exported symbol documented. Non-obvious branches have `# why:` comments. No commented-out code.
- Commits: Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `spec:`) scoped to pillar/FM e.g. `feat(P01): FM-005 idempotent case intake`.
- Branch: `NNN-short-name` from `main` (e.g. `001-case-management-platform`).
- Migrations: `alembic revision --autogenerate` with both `upgrade` and `downgrade`, portable SQL, `tenant_id` on every FK where applicable.

## 12. Integration Seams — Where to Plug Corporate/Azure Later

Central inventory lives in `docs/integrations/seams.md` (create if missing); each seam also annotated in code with `# INTEGRATION_SEAM: <target> — adapter contract, replaced when corp env available`.

| Seam | Port (`domain/ports.py` or `application/ports.py`) | Local adapter | Azure production adapter | Location marker |
|---|---|---|---|---|
| Relational DB | `CaseRepository`, `*Repository` | `infrastructure/persistence/repositories/*` (asyncpg) | `infrastructure/persistence/azure_sql_*` (Azure SQL) | `app/db.py:39` |
| Document store | `DocumentStore` | `infrastructure/storage/local_document_store.py` | `AzureBlobDocumentStore` | `config.documents_path` |
| Messaging | `MessageBus` + outbox dispatcher | `infrastructure/messaging/local_message_bus.py` + `outbox_dispatcher.py` | `AzureServiceBus` | `plan.md:102` |
| Identity | `IdentityProvider` (`deps.py:58`) | `LocalHeaderIdentityProvider` (`deps.py:62`) | `EntraIdentityProvider` | `deps.py:93` |
| Secrets | `SecretProvider` | `infrastructure/secrets/local_secret_provider.py` | `AzureKeyVault` | `config.py` |
| Telemetry | `Telemetry` | `infrastructure/telemetry/structured_logging.py` (structlog) | OTLP -> Azure Monitor/App Insights | `main.py:25` |
| AI assistance | `application/ai/*` Protocols | no-op stub | `infrastructure/ai/azure_openai_*` | §4.R11 |
| Enterprise imports | `ImportJobRepository` | synthetic CSV/JSONL fixtures | OnBase/Panzura connectors | FM-026..FM-030 (Gated) |

When adding a seam: (1) define Protocol in `domain/` or `application/`; (2) implement local adapter in `infrastructure/`; (3) wire via `app/config.py` + `app/deps.py` + `app/main.py`; (4) add stub/placeholder dir for Azure adapter with `README.md` describing env vars/config; (5) log at startup which adapter is active (`/api/v1/health`).

## 13. Architecture Documentation (Mermaid) — Required on Completion

After any feature slice converges, write `specs/NNN-short-name/architecture.md` with at minimum:

```mermaid
flowchart LR
  UI[React /api client] --> API[FastAPI /api/v1]
  API --> App[Application use-cases]
  App --> Domain[Domain entities + rules]
  App --> Ports[Ports/Protocols]
  Ports --> Infra[Infra adapters - Postgres / FS / In-proc bus / Header IdP]
  App --> Outbox[(Outbox + Audit hash chain)]
  Ports -. future .-> Azure[(Azure SQL / Blob / ServiceBus / Entra / KeyVault)]
```

Include: module map, domain model ER sketch, sequence for core mutation (case creation with idempotency+outbox+audit in one transaction), guard evaluation trace, and a table of seams above with status.

## 14. Definition of Done (slice)

Derived from Constitution + `specs/001-.../first-slice/tasks.md:169`:

- [ ] ≥1 P1/P2 user story independently testable and automated (spec.md scenarios)
- [ ] Domain + application behavior implemented with audit + authorization + idempotency + tenant isolation in same transaction where required
- [ ] Failure behavior: retries, duplicate delivery, partial failure, timeouts, poison messages handled (no silent loss; outbox replayable)
- [ ] Tests: `AC-001..AC-010` style — coverage ≥80%, tenant-isolation, audit immutability, cycle validation (human cycles allowed, automatic cycles rejected), synthetic marker visible
- [ ] API + OpenAPI documented, frontend types generated, pnpm build green
- [ ] Migrations reversible, portable types, no pg-specific ops
- [ ] Decisions recorded (§4.R8), Mermaid in `specs/NNN-*/architecture.md` (§4.R9), health distinguishes local vs azure
- [ ] No PHI/secrets in fixtures/logs/payloads; `SYNTHETIC ENVIRONMENT` marker present
- [ ] `ruff` + `mypy --strict` + `import-linter` + `pytest` all green

## 15. Quick Commands for Agents

```bash
# explore without guessing
codegraph explore "CaseRepository, next_reference, DocumentStore"

# verify layers before commit (non-negotiable)
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict app/domain app/application && uv run lint-imports  # then pytest

# local audit — do NOT assume Docker Desktop / healthy WSL; CLI may work but WSL may error
docker info; docker compose version; docker compose ps
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q --cov=app/domain --cov=app/application --cov-report=term-missing

# orchestration (lightweight dag; every command also accepts --json)
dag status --json
dag init --objective "FM-005: case intake idempotent"
dag add --title "..." --spec "Inputs: ... Outputs: ... Acceptance: ..."
dag add --title "..." --spec "..." --deps task_abc123,task_def456
dag list --json
dag serve --open   # viewer on :8787
```

## 16. Design Schema & Data Contracts (the portable core)

Agents must not invent schema dialect. All tables live in `backend/app/infrastructure/persistence/models.py` using SQLAlchemy **generic** types only (`plan.md:289`, `AGENTS.md:34`). Each table includes `tenant_id` + `created_at/updated_at` (UTC from `Clock`).

### 16.1 Canonical tables (first slice)

```
tenants(id, name, created_at)
users(id, tenant_id FK, display_name, roles JSON, created_at)
case_types(id, tenant_id, key, name, description, current_version_id FK, sensitivity_default)
case_type_versions(id, case_type_id FK, version_number, schema_json JSON, status [draft|published|retired], published_at, published_by)
cases(id, tenant_id, case_type_version_id FK, reference Text unique per tenant, status, priority, owner_user_id, owner_queue_id, current_workflow_state, data_json JSON, sensitivity, version_token Integer, created_at, updated_at, closed_at)
workflow_definitions(id, tenant_id, key, name, current_version_id)
workflow_versions(id, workflow_definition_id FK, version_number, definition_json JSON, status [draft|published|retired], published_at)
workflow_instances(id, tenant_id, case_id FK, workflow_version_id FK, current_state, correlation_id, created_at)
workflow_transitions(id, tenant_id, workflow_instance_id FK, from_state, to_state, transition_key, actor_id, occurred_at, payload_json)
tasks(id, tenant_id, case_id FK, type, title, status [open|completed|cancelled], assignee_user_id, assignee_queue_id, priority, due_at, idempotency_key, completed_at, completion_outcome)
documents(id, tenant_id, case_id FK, file_name, content_type, classification, sensitivity, sha256, size, current_version, storage_ref, uploaded_by, uploaded_at)
document_versions(id, document_id FK, version, sha256, size, storage_ref, uploaded_by, uploaded_at)
participants(id, tenant_id, case_id FK, role, display_name, external_ref, source, is_primary, added_at, removed_at)
decisions(id, tenant_id, case_id FK, decision_type, outcome, rationale, evidence_json, made_by, made_at, applicable_workflow_version_id, supersedes_id)
audit_events(id, tenant_id, event_type, occurred_at, actor_id, actor_type, subject_type, subject_id, case_id, correlation_id, causation_id, payload_json, prev_hash, hash)  # append-only, hash chain
configuration_publications(id, tenant_id, artifact_type, artifact_id, version_id, action, actor_id, occurred_at, change_summary)
outbox(id, tenant_id, event_type, event_id, payload_json, created_at, dispatched_at, attempt_count, last_error)
idempotency_keys(key, tenant_id, request_hash, response_status, response_body, created_at, expires_at)
```

Naming convention `app/db.py:22` (`pk_*`, `fk_*`, `ix_*`). Migrations via `alembic revision --autogenerate` with **both** `upgrade`/`downgrade`, reversible, tenant FKs indexed. Never `gen_random_uuid()` or `JSONB`.

### 16.2 Declarative JSON contracts (config, not code)

**CaseType `schema_json`** (`P05`, `domain/case_type.py:1`, `domain/case.py:171`):

```json
{
  "fields": [
    {"key":"title","label":"Title","type":"text","required":true,"sensitivity":"internal","searchable":true},
    {"key":"priority","label":"Priority","type":"enum","required":true,"options":["low","normal","high","urgent"]},
    {"key":"due_date","label":"Due date","type":"date","required":false}
  ],
  "document_classifications": ["Contract","Correspondence","Evidence"],
  "rules": []
}
```

Stable `key` is the identity; `label` may rename across versions without breaking history (`P05-US8`). Validation at write time only (`case.py:171`).

**Workflow `definition_json`** (`P02`, `plan.md:211`):

```json
{
  "states": [{"key":"intake","is_initial":true},{"key":"in_review"},{"key":"decided"},{"key":"closed","is_terminal":true}],
  "transitions": [{
    "key":"submit_for_review","from":"intake","to":"in_review",
    "guard":{"all":[{"field":"case.participants","op":"exists","min":1},{"field":"case.documents","op":"exists","min":1}]},
    "actions":[{"type":"create_task","task_type":"review","queue":"review_queue"},{"type":"start_sla","sla_key":"review_due","hours":48}],
    "human_driven": true
  }]
}
```

**Rule evaluator** (`domain/rules.py`, `plan.md:244`): operators `all/any/not/eq/ne/in/gt/gte/lt/lte/exists/contains/regex`. Pure function, no I/O, nesting depth capped at publish validation. `human_driven` flag lets the validator allow human cycles (`intake->in_review->needs_info->in_review`) while rejecting automatic loops (`plan.md:235`).

### 16.3 Invariants agents must preserve

- `CaseReference` monotonic per tenant (`C-{TENANT_SHORT}-{YYYY}-{SEQ:06d}`, `case.py:42`, `ports.py:37`), issued by `CaseRepository.next_reference`.
- `CaseTypeVersion` / `WorkflowVersion` immutability on `publish()` — any UPDATE to `status=published` must raise (`case_type.py`, `workflow.py:66`).
- `version_token` optimistic concurrency (`case.py:156`, `edit_case.py:44`): `If-Match` required, stale token → `409 Conflict`.
- Audit hash `hash = SHA-256(prev_hash + canonical(payload_json))` (`domain/audit.py:26`), same transaction as domain change + outbox row (`T028/T032`). No partial commit.
- Every repository/query includes `WHERE tenant_id = :tenant_id` (`ports.py:26`). Defense-in-depth listener (`config.TENANT_LISTENER_ENABLED`) never sole mechanism.

## 17. API & Frontend Contracts

### 17.1 HTTP API shape

Pydantic schemas live in `interfaces/http/schemas/*.py`; routers in `interfaces/http/routers/*.py`; error model in `interfaces/http/errors.py`.

```
POST   /api/v1/cases                          Idempotency-Key required, returns CaseResponse + reference
GET    /api/v1/cases/{id}                     tenant-scoped, auth-checked
PATCH  /api/v1/cases/{id}                     If-Match: <version_token>
POST   /api/v1/cases/{id}/transitions         {transition_key, payload}
GET    /api/v1/cases/{id}/timeline            paginated, actor/type/date filters
POST   /api/v1/cases/{id}/documents           multipart, classification + sha256
GET    /api/v1/documents/{id}/content         Content-Disposition, auth-checked
GET    /api/v1/tasks?assignee=me&status=open  paginated
POST   /api/v1/tasks/{id}/complete            Idempotency-Key required
POST   /api/v1/cases/{id}/decisions
GET    /api/v1/admin/case-types  /workflows   draft→validate→publish→retire lifecycle
POST   /api/v1/admin/workflows/{id}/versions/{n}/simulate   deterministic trace
GET    /api/v1/health                         {adapters:{db:"local-postgres"|"azure-sql", store:"local-fs"|"azure-blob", bus:"local-outbox"|"service-bus", idp:"local-header"|"entra"}, synthetic_environment: bool}
```

Shared envelope:

```python
class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None
    limit: int  # default 50, max 200
class ErrorResponse(BaseModel):
    code: str           # e.g. validation_failed, conflict, forbidden, not_found, dependency_unavailable
    message: str
    details: dict | None  # safe only — never SQL/stack/secrets
    correlation_id: str
```

Pagination, correlation (`main.py:39`, `deps.py:38`), idempotency (`idempotency.py`), and tenancy are middleware/deps, not domain logic.

### 17.2 Frontend contract

`frontend/src/api/` is **generated**, never hand-typed:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/api/types.gen.ts
```

Stack: React 18 + Vite + TanStack Query + React Router (`frontend/package.json:6`, `vite.config.ts` proxies `/api` → `:8000`). Query keys are `["cases", id]`, `["tasks", filters]`, `["timeline", caseId]`. Case workspace route `/cases/:id` renders summary + participants + documents + tasks + timeline. Keep components under `src/cases|tasks|documents|audit|workflow|shell/`.

## 18. Synthetic Data, Identity & Observability

- **Synthetic only** (`constitution.md:471`, `plan.md:259`): seed via `app/cli/seed.py` (Typer) — creates tenant `ACME`, users `intake/reviewer/supervisor`, `CaseType ServiceRequest`, workflow `intake→in_review→decided→closed`, one demo case. All fixtures use `example.test` domain, obviously fake names, no DOBs, no PHI. `SYNTHETIC_ENVIRONMENT=1` logged on startup (`main.py:95`) and shown in UI banner.
- **Identity** (`deps.py:62`): `LocalHeaderIdentityProvider` reads `X-Dev-User`/`X-Dev-Tenant` only when `APP_ENV=local`; otherwise `401`. `UserContext(user_id, tenant_id, roles, is_system)` threaded through `get_current_user`. Fail-closed (`deps.py:98`).
- **Telemetry** (`infrastructure/telemetry/structured_logging.py`, `correlation.py`): every log line includes `tenant_id`, `correlation_id`, `actor_id`, `method`, `path`; correlation from `X-Correlation-Id` or `uuid7()` (`deps.py:45`). Redaction list for `ssn`, `phi`, `secret`, `password`. `log_level` via `config.py:47`.
- **Failure** (`constitution.md:398`): mutations are idempotent + outbox-atomic. Background `outbox_dispatcher.py` drains with retry/backoff, dead-letter after N attempts, replayable via admin endpoint. SLA scanner (`sla_clocks`) likewise. Operators diagnose via `/api/v1/health` + timeline + `verify-audit` CLI that replays hash chain.

## 19. Agent Heuristics — How to Slice Orchestration

- **Task size:** 4-12 tasks per Run, each touching ≤3 files + its tests. If a task would exceed 600 lines, split it. Use self-contained `--spec` template:
  ```
  Inputs: read <files>, depends on <task_id outputs>
  Outputs: create/modify <files>
  Acceptance: <command that must pass> + observable behavior
  ```
  Imperative, no "as appropriate" or "investigate".
- **Dependency shape:** skeleton → foundation hardening → domain → application → infrastructure → interfaces → frontend/seed. Tasks are parallel the moment their `--deps` are satisfied — only declare a dep when the consumer truly needs the producer's outputs. Validate with `dag list` / `dag layers` after each batch.
- **Gates:** Add one gate after design/spec approval and one before publish/merge (`dag gate --id <task_id> --question "Approve ...?"`), then resolve it (`dag approve --id <task_id>` / `dag reject --id <task_id>`). Don't orphan gates — resolve before run end or set `dag settings --gates skip` deliberately.
- **Skepticism loop (every task-done):** `ruff check + ruff format --check && mypy --strict app/domain app/application && lint-imports verify && APP_ENV=test DATABASE_URL='sqlite+...` pytest -q && pnpm typecheck` . If any fails, fix before marking done.
- **Seams & decisions:** When you introduce a Protocol/adapter, immediately add `# INTEGRATION_SEAM: <AzureTarget>` comment and an entry in `docs/integrations/seams.md` with env vars needed; write a 5-line ADR in `docs/decisions/ADR-NNN-*.md` (Context/Options/Decision/Consequences/Seam).
- **Mermaid on converge:** `specs/NNN-*/architecture.md` must exist before closing the Run — includeER sketch, sequence for `POST /cases` (idempotency → repo → audit hash → outbox → commit) and guard trace. CI enforces presence.

---

*Build locally. Architect for Azure. Connect to Azure later.* Secrets and PHI handling per Constitution V; deterministic core before AI per XIV. If a requirement would force big-bang migration, treat it with suspicion (XI); prefer vertical slices (XIII) over horizontal infrastructure projects.
