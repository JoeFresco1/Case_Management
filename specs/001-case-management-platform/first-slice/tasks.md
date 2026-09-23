# Tasks: Case Management Platform — First Substantial Slice (Phases 0–3)

**Feature Branch**: `001-case-management-platform`
**Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Roadmap**: [../../pillars/00-product-roadmap.md](../../pillars/00-product-roadmap.md)
**Created**: 2026-08-29
**Last revised**: 2026-08-31 (added pillar cross-references)
**Scope of this task list**: **Phases 0–3 only** (foundation, case+timeline, documents+tasks, workflow+rules). Phases 4–7 (decisions/audit-hardening, migration/integration, Azure adapters, production adoption) are separate future specs.

Phases 0–3 deliver the target vertical:
> **Create Case → Workflow Starts → Task Created → Worker Opens Case → Document Added → Task Completed → Rule Evaluated → Workflow Transition → Decision → Case Closed → Timeline Explains Everything**

## Pillar ownership

Every task in this list is owned by one or more pillars. The pillar is the authority for scope, user stories, and acceptance scenarios. When in doubt, the pillar spec wins.

| Phase | Pillar(s) primarily affected |
|---|---|
| Phase 0 (Foundation) | P11 (security/identity foundations), P13 (API + error model), P09 (audit port) |
| Phase 1 (Case + timeline) | P01, P05, P09, P11, P13 |
| Phase 2 (Documents + tasks) | P03, P02 (tasks) |
| Phase 3 (Workflow + rules) | P02, P10, P05 (rule language) |

The task list is reordered so a **walking skeleton runs early**: a synthetic case can be created, fetched, and viewed in the browser before the rest of the plumbing is hardened. Foundational requirements that the skeleton needs (DB, configuration, basic ports, the case domain, the case-type domain, HTTP, seed) come first. Hardening (audit, idempotency, tenant isolation, telemetry, outbox, documents, tasks, workflow sophistication) comes second.

Tasks marked **[P]** are parallelizable with the prior task. Tasks marked **[WS]** are part of the walking-skeleton milestone — the first checkpoint at which `POST /cases` works end-to-end with a synthetic case viewable in the browser.

---

## Phase 0 — Foundation (with walking skeleton)

### Walking-skeleton milestone

- [x] **T001 [WS]** Create repo layout per plan: `backend/app/...`, `frontend/src/...`, `docker-compose.yml`, `pyproject.toml`, `package.json`, `README.md`, `.gitignore`, `.env.example`, `.python-version` (3.12), `.nvmrc` (24). (Tool config — ruff, mypy, import-linter — lives consolidated in `backend/pyproject.toml`, not separate `mypy.ini`/`ruff.toml`/`.import-linter.ini` files.)
- [x] **T002 [WS]** `docker-compose.yml` with a single `postgres:16-alpine` service on `localhost:5432`, persistent volume, healthcheck. Add `DATABASE_URL` to `.env.example`.
- [x] **T003 [WS]** Backend `pyproject.toml` (Python 3.12): runtime deps `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `pydantic>=2`, `pydantic-settings`, `structlog`, `python-multipart`, `httpx`, `typer`; dev deps `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `import-linter`, `freezegun`.
- [x] **T004 [WS]** Frontend `package.json`: Vite + React 18 + TypeScript, `react-router`, `@tanstack/react-query`, `openapi-typescript` (dev), `vitest`, `@testing-library/react`, `eslint`, `prettier`. Vite proxy `/api` to `http://localhost:8000`.
- [x] **T005 [WS]** `app/config.py` Pydantic Settings: `app_env` (local|test|shared|prod), `database_url`, `documents_path`. Identity mode is derived from `app_env` (no separate field): dev-header identity (`X-Dev-User`/`X-Dev-Tenant`) is honored only when `app_env == local`. Fail-closed: non-local requests presenting dev headers are refused per-request (401).
- [x] **T006 [WS]** `app/db.py` async SQLAlchemy engine + session factory. `app/deps.py` with `get_db`, `get_clock`, `get_identity`, `get_correlation_id`.
- [x] **T007 [WS]** Alembic init (async template); first migration creates `tenants`, `users`. (Other tables land with their owning tasks below.)
- [x] **T008 [WS]** `app/domain/ports.py` Protocols: `CaseRepository`, `CaseTypeRepository`, `CaseTypeVersionRepository`, `DocumentRepository`, `TaskRepository`, `WorkflowInstanceRepository`, `WorkflowVersionRepository`, `ParticipantRepository`, `AuditEventStore`, `DocumentStore`, `MessageBus`, `SecretProvider`, `Telemetry` (plus follow-on ports). (`IdentityProvider` lives in `app/deps.py`, `Clock` in `app/domain/context.py`; `DecisionRepository`, `ConfigurationRepository`, `ImportJobRepository` are intentionally absent — decisions → Phase 4, enterprise imports → Phase 5, configuration reuses case-type/workflow repos.)
- [x] **T009 [WS]** `app/domain/case_type.py` `CaseType` + `CaseTypeVersion` with `Status` lifecycle (draft|published|retired). `publish()` is irreversible.
- [x] **T010 [WS]** `app/domain/case.py` `Case` aggregate: invariants (reference uniqueness, status transitions), `CaseStatus` enum, `CaseReference` value object with monotonic per-tenant sequence. Pure dataclass.
- [x] **T011 [WS]** `app/infrastructure/persistence/models.py` SQLAlchemy ORM models for `tenants`, `users`, `case_types`, `case_type_versions`, `cases` only. All FKs include `tenant_id`. All column types use SQLAlchemy generic types (`Uuid`, `JSON`, `DateTime(timezone=True)`, `Text`, `String`).
- [x] **T012 [WS]** `app/infrastructure/persistence/repositories/case_repository.py` and `case_type_repository.py`: explicit `tenant_id` argument on every method; `WHERE tenant_id = :tenant_id` in every query.
- [x] **T013 [WS]** `app/application/cases/create_case.py`: validates payload, loads `CaseTypeVersion`, validates required fields, generates `CaseReference`, creates `Case` in initial state. Returns the case. (No workflow instance, no audit, no outbox yet — that lands in T018+.)
- [x] **T014 [WS]** `app/application/cases/view_case.py`: assembles a case summary. (Authorization redacts in T031.)
- [x] **T015 [WS]** `app/interfaces/http/routers/cases.py`: `POST /api/v1/cases`, `GET /api/v1/cases/{id}`. Pydantic request/response schemas. OpenAPI tags.
- [x] **T016 [WS]** `app/main.py` FastAPI app factory: mount `/api/v1`, request log middleware (correlation id from `X-Correlation-Id` or generated UUIDv7), basic `ErrorResponse` model.
- [x] **T017 [WS]** `app/cli/seed.py` (Typer): creates one tenant, three users (intake/reviewer/supervisor), one `CaseType` ("ServiceRequest") with a published `CaseTypeVersion`. Logs "SYNTHETIC ENVIRONMENT" marker.
- [x] **T018 [WS]** `app/cli/seed.py` extends: seeds one in-flight case in the initial state so the browser has something to open.
- [x] **T019 [WS]** Frontend: Vite app boots, case workspace route (`/cases/:id`) calls `GET /api/v1/cases/{id}` and renders the summary.
- [x] **T020 [WS]** README: documented startup (compose up → migrate → seed → serve per `README.md`; sqlite profile when Docker is unavailable). **`uv run pytest` and `pnpm dev` both work; the seeded case is openable in the browser.** This is the walking-skeleton checkpoint.

### Walking-skeleton checkpoint

At the end of T001–T020, the system satisfies:
- `docker compose up -d && uv sync && uv run alembic upgrade head && uv run python -m app.cli seed && uv run uvicorn app.main:app --reload && pnpm dev`
- `POST /api/v1/cases` creates a synthetic case.
- `GET /api/v1/cases/{id}` returns the case.
- The browser shows the case summary.
- No audit, no idempotency, no outbox, no workflow engine, no documents, no tasks yet — those are added next without breaking the skeleton.

### Harden the foundation

- [x] T021 `app/interfaces/http/errors.py`: `ErrorResponse` model with `code`, `message`, `details`, `correlation_id`. Exception handlers map domain errors to structured HTTP responses (no stack traces, no SQL, no secrets).
- [x] T022 `app/interfaces/http/idempotency.py` middleware: `Idempotency-Key` header on POSTs; stores `(tenant_id, key, request_hash, response)` in `idempotency_keys`; replays prior response on duplicates with same request hash; rejects mismatched hash with 409. Migration adds the table.
- [x] T023 `app/interfaces/http/pagination.py`: `Page[T]` response with `items`, `next_cursor`, `limit`; default 50, max 200.
- [x] T024 `app/infrastructure/persistence/models.py` adds `idempotency_keys`. Migration.
- [x] T025 `app/infrastructure/persistence/models.py` adds `audit_events`, `outbox`, `configuration_publications`. Migrations.
- [x] T026 `app/domain/audit.py` `AuditEvent` value object: `compute_hash(prev_hash, payload)` using canonical JSON; `AuditChain` helper for replay verification.
- [x] T027 `app/domain/events.py` `DomainEvent` base + `CaseCreated`, `CaseUpdated`, `WorkflowTransitioned`, `DocumentAdded`, `DecisionRecorded`, `TaskCreated`, `TaskCompleted`, `AuditCorrectionRecorded`. Outbox row schema.
- [x] T028 Outbox write in the same transaction as the domain change: use cases stage rows via `OutboxWriter.enqueue` on the request session (`SqlOutboxWriter`), committed atomically with the domain change. `app/infrastructure/messaging/local_message_bus.py` is the dispatcher-driven in-process fan-out (production seam: Azure Service Bus).
- [x] T029 `app/infrastructure/messaging/outbox_dispatcher.py`: background task draining the outbox with retry/backoff and dead-letter status.
- [x] T030 `app/application/audit/record_event.py`: writes an `audit_events` row in the current transaction. Idempotent on (event_type, correlation_id, payload_hash).
- [x] T031 `app/application/authorization.py`: `Policy` registry; `policy.evaluate(action, UserContext, Resource) -> Allow|Deny`. Server-side. Default-deny. Wired into `view_case` (T014) and `create_case` (T013) first.
- [x] T032 `app/application/cases/create_case.py` extended: writes `CaseCreated` audit + outbox row in the same transaction as the case insert.
- [x] T033 `app/application/audit/view_timeline.py`: chronological list of `audit_events` for a case with pagination.
- [x] T034 `app/interfaces/http/routers/audit.py`: `GET /api/v1/cases/{id}/timeline`. Authorization-enforced.
- [x] T035 Audit immutability: SQLAlchemy event listener that raises on any UPDATE/DELETE of an `AuditEvent` instance. No application path may attempt to modify or delete an `AuditEvent`. Corrections are written as a new `AuditCorrectionRecorded` event. **There is no admin path that mutates audit history.**
- [x] T036 `app/infrastructure/telemetry/structured_logging.py` + `correlation.py`: middleware sets `correlation_id`; every log line includes `tenant_id`, `correlation_id`, `actor_id`. Redaction list for known sensitive keys.
- [x] T037 `app/infrastructure/persistence/models.py` adds `tenants.idempotency_keys.tenant_id` NOT NULL; orphan-row test rejects inserts without `tenant_id` (defense in depth).
- [x] T038 Tenant-isolation tests: every repository test parameterized with two tenants; cross-tenant reads/writes must fail.
- [x] T039 `import-linter` config enforcing: `domain` imports nothing from `fastapi`/`sqlalchemy`/`infrastructure`; `application` imports `domain` only; `interfaces` does not import `infrastructure` directly. CI fails on violation.
- [x] T040 CI workflow: lint (ruff), typecheck (mypy `--strict` on `app/domain` and `app/application`), import-lint, pytest, frontend build. Matrix Python 3.12, Node 20.
- [x] T041 `app/interfaces/http/routers/health.py`: returns adapter inventory per port, distinguishing `local` from `azure-prod`. FR-034.
- [x] T042 `README.md`: documents the local demo flow end-to-end and the "SYNTHETIC ENVIRONMENT" marker.

### Foundation acceptance

- T001–T042 done. Walking-skeleton checkpoint still passes. `pytest` passes with ≥ 80% coverage on `app/domain` and `app/application`. All 8 spec scenarios that don't require documents, tasks, or workflow (A, partial B, M, N) pass as automated tests.

---

## Phase 1 — Case and timeline core (hardening)

- [x] T043 `app/domain/case.py` extended: optimistic concurrency token (`version_token`); `edit()` returns conflict on stale token. Pure domain.
- [x] T044 `app/application/cases/edit_case.py`: requires `If-Match: <version_token>`; rejects with 409 on stale token; writes `CaseUpdated` audit + outbox in same transaction.
- [x] T045 `app/interfaces/http/routers/cases.py` adds `PATCH /api/v1/cases/{id}`.
- [x] T046 `app/domain/participant.py`: `Participant` value object with role, display name, source, is_primary, added_at, removed_at.
- [x] T047 `app/application/participants/manage_participants.py`: add/update/remove; historical references preserved on remove.
- [x] T048 `app/interfaces/http/routers/participants.py`: add/update/remove endpoints on `/cases/{id}/participants`.
- [x] T049 `app/infrastructure/persistence/models.py` adds `participants` table. Migration.
- [x] T050 Frontend: case list page (`/cases`) with pagination.
- [x] T051 Frontend: create-case form using the published case type's schema; client-side validation mirrors server-side.
- [x] T052 Frontend: case workspace shows participants, structured data, timeline panel.

### Phase 1 acceptance

- User Stories 1, 2, 6, 8 fully passing. Scenarios A, B, C (with required-field guard stub), G, J (decision portion deferred to Phase 4), M, N passing.

---

## Phase 2 — Documents and tasks

- [x] T053 `app/domain/document.py` `Document` + `DocumentVersion`. Invariant: a document's current version is a non-null reference to one of its versions.
- [x] T054 `app/infrastructure/storage/local_document_store.py`: content-addressable filesystem layout `documents/{tenant_id}/{sha256[0:2]}/{sha256}`, returns `DocumentRef`; rejects writes outside `documents_path` (path-traversal guard).
- [x] T055 `app/application/documents/upload_document.py`: validates file type/size, computes SHA-256, writes content through `DocumentStore`, creates `Document` + `DocumentVersion` + `DocumentAdded` audit + outbox row in one transaction. Rejects duplicate uploads (same `sha256` for same case) when configured.
- [x] T056 `app/application/documents/download_document.py`: authorization check; returns content with `Content-Disposition: attachment; filename="..."` and `Content-Type` from metadata. Audit access.
- [x] T057 `app/interfaces/http/routers/documents.py`: `POST /api/v1/cases/{id}/documents` (multipart), `GET /api/v1/documents/{id}/content`.
- [x] T058 `app/infrastructure/persistence/models.py` adds `documents`, `document_versions`. Migrations.
- [x] T059 `app/domain/task.py` `Task` aggregate: status enum, idempotent completion (requires `Idempotency-Key`).
- [x] T060 `app/domain/queue.py` `Queue` value object (id, key, name, assignment_policy). For v1: `manual` and `round_robin` only.
- [x] T061 `app/application/tasks/complete_task.py`: idempotent, validates required inputs, writes `TaskCompleted` audit + outbox. If a workflow transition is configured to fire on task completion, calls `execute_transition` in the same transaction.
- [x] T062 `app/interfaces/http/routers/tasks.py`: `GET /api/v1/tasks?assignee=me|queue:<key>&status=open|all`, `POST /api/v1/tasks/{id}/complete`.
- [x] T063 `app/infrastructure/persistence/models.py` adds `tasks`, `queues`. Migrations.
- [x] T064 Frontend: document upload widget in case workspace; document list with redaction-aware preview.
- [x] T065 Frontend: task list page (`/tasks`) with filtering; task detail + complete action.

### Phase 2 acceptance

- User Stories 3 (partial — task completion wires into the workflow once T067 lands), 4 passing. Scenarios C, F, K passing.

---

## Phase 3 — Workflow and rules (with simulator)

- [x] T066 `app/domain/workflow.py` `WorkflowDefinition` + `WorkflowVersion` with immutability on publish.
- [x] T067 `app/domain/rules.py` `evaluate(condition, context)` for declarative JSON conditions. Operators: `all`, `any`, `not`, `eq`, `ne`, `in`, `gt`, `gte`, `lt`, `lte`, `exists`, `contains`, `regex`. Pure function, no I/O. Unit tests cover every operator + nesting + type errors.
- [x] T068 `app/domain/sla.py` `SlaClock`: start/pause/resume/breach; backed by a simple `sla_clocks` table.
- [x] T069 `app/infrastructure/persistence/models.py` adds `workflow_definitions`, `workflow_versions`, `workflow_instances`, `workflow_transitions`, `sla_clocks`, `decisions`. Migrations.
- [x] T070 `app/application/workflow/start_workflow.py`: creates a `WorkflowInstance` for a case from the published workflow version, enters initial state, creates configured initial tasks, starts configured timers. Called by `create_case` (T032).
- [x] T071 `app/application/workflow/execute_transition.py`: load workflow version, find transition by `key`, evaluate guard, execute configured actions (create task, start SLA, write field), write `WorkflowTransitioned` audit + outbox, all in one transaction. Reject with structured error on guard failure (FR-010). Distinguish "human-driven" vs "automatic" transitions in the workflow JSON so the cycle validator can reason about them.
- [x] T072 `app/application/configuration/validate_definition.py`: rejects unreachable states, ambiguous transitions, missing terminal paths, invalid guard operators, unknown queue refs, states with no possible exit, and **non-terminating automatic cycles** (cycles composed entirely of automatic transitions with no breakable guard). **Human-driven cycles are allowed** (e.g., `intake → in_review → needs_info → in_review`).
- [x] T073 `app/application/workflow/publish_workflow.py`: runs validation, transitions lifecycle, emits `ConfigurationPublished` event. Idempotent on (workflow_id, version_number).
- [x] T074 `app/application/workflow/simulate_workflow.py`: deterministic simulator. `simulate(workflow_version, synthetic_case, actions: list[Action]) -> SimulationResult`. Returns:
  - `initial_state`
  - `steps`: per simulated transition, `{ transition, from, to, guards: [{rule, inputs, result}], actions_fired: [...] }`
  - `final_state`
  - `outcome`: `completed | blocked | exception`
  - `blockers`: list of unmet guard conditions (for blocked outcome)
  Used by:
  - Tests (every workflow in the seed is simulated through its happy path and a forced-blocked path).
  - An admin dry-run endpoint that runs a workflow version against a synthetic case.
  - A bulk runner that runs N synthetic scenarios and reports counts (this is the future "150 scenarios before publish" capability; v1 ships the simulator and the per-workflow run, deferring the bulk runner to a Phase 5 spec).
- [x] T075 `app/interfaces/http/routers/admin_workflows.py`: `POST /api/v1/admin/workflows`, `POST /api/v1/admin/workflows/{id}/versions`, `POST /api/v1/admin/workflows/{id}/versions/{n}/validate`, `POST .../publish`, `POST /api/v1/admin/workflows/{id}/versions/{n}/simulate`. Rejects mutation of published versions.
- [x] T076 `app/interfaces/http/routers/cases.py` adds `POST /api/v1/cases/{id}/transitions` (execute transition with optional payload).
- [x] T077 `app/cli/seed.py` extended: seeds a published `WorkflowVersion` for "ServiceRequest" with the canonical vertical: `intake → in_review → decided → closed`, configured transitions for `submit_for_review`, `record_decision`, `close`. Initial state `intake`. Initial task "Review case" assigned to `review_queue` (created in T063). Initial SLA 48h.
- [x] T078 Background worker for SLA: scans `sla_clocks` every minute, fires warning/breach actions, writes `SlaWarningRaised` / `SlaBreached` events.
- [x] T079 Frontend: workflow admin (read-only in v1 — list, view version, view diff vs previous, run simulator and display step trace).
- [x] T080 Frontend: case workspace adds a "Transitions" panel that lists available transitions and their guard results; on click, executes the transition and refreshes the timeline.

### Phase 3 acceptance

- All 8 user stories fully passing. All 14 spec scenarios (A–N) except those that explicitly depend on Phase 4+ (L — dry-run import — deferred) and a few that are partial until Phase 4 (J — decision portion). The full vertical works:
  - Create Case → Workflow Starts → Task Created → Worker Opens Case → Document Added → Task Completed → Rule Evaluated → Workflow Transition → Decision (Phase 4) → Case Closed → Timeline Explains Everything.

---

## Cross-cutting acceptance (must all pass before this slice is "done")

- [x] **AC-001** `docker compose up -d && uv run pytest` passes with ≥ 80% coverage on `app/domain` and `app/application`.
- [x] **AC-002** `ruff check`, `mypy --strict app/domain`, `import-linter verify` all pass in CI.
- [ ] **AC-003** Walking-skeleton checkpoint (T001–T020) passes on a clean checkout in under 10 minutes with no outbound network calls to corporate endpoints.
- [x] **AC-004** Every workflow in the seed has a passing simulator trace (happy path + at least one forced-blocked path) committed under `tests/workflows/simulations/`.
- [x] **AC-005** A second tenant created in seed data cannot see, edit, or be searched for the first tenant's cases (tenant-isolation test).
- [x] **AC-006** Synthetic environment marker is visible in logs and on the web UI.
- [x] **AC-007** No real PHI, secrets, or production identifiers appear in any seed fixture, log line, or audit event.
- [x] **AC-008** Audit immutability is verified by a test that attempts UPDATE/DELETE on `audit_events` through the ORM and through raw SQL (using a non-migration role) and asserts both fail. Correcting an event is demonstrated as a new `AuditCorrectionRecorded` row referencing the original.
- [x] **AC-009** No PostgreSQL-specific types, operators, or SQL in the `domain` or `application` layers. A grep-based CI check enforces this.
- [x] **AC-010** Human-driven cycles are allowed; an explicit test asserts a workflow like `intake → in_review → needs_info → in_review → decided` publishes successfully.

> **AC-003 validation note (2026-09-09):** not runnable in this environment — the Docker daemon is unreachable (`//./pipe/docker_engine` missing; daemon not running) and the `compose` plugin is absent, so `docker compose up -d` cannot start Postgres here. To validate: on a host with a working daemon, run `docker compose up -d`, then the walking-skeleton checkpoint (seed → create case → fetch case → open in browser) from a clean checkout, confirming completion in under 10 minutes with no outbound corporate calls. Backend suite (1,147 tests) and frontend suite (40 tests) pass without Docker via the sqlite test profile.

---

## Deferred to later specs (NOT part of this slice)

- **Phase 4** — Decisions hardening, audit-chain verification CLI, operational health reports, timeline filtering, tenant-isolation test hardening.
- **Phase 5** — Migration / import pipeline, synthetic import package, outbox hardening, search, dry-run import (Scenario L).
- **Phase 6** — Azure adapter scaffolding (Protocols only, no live connection), `adapter_overrides` config, Azure adapter contract tests.
- **Phase 7** — Production adoption, security review, real Azure deployment, AI features.
