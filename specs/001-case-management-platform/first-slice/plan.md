# Implementation Plan: Case Management Platform — First Substantial Slice

**Feature Branch**: `001-case-management-platform`
**Spec**: [spec.md](./spec.md)
**Constitution**: [../../.specify/memory/constitution.md](../../.specify/memory/constitution.md)
**Roadmap**: [../../pillars/00-product-roadmap.md](../../pillars/00-product-roadmap.md)
**Created**: 2026-08-29
**Last revised**: 2026-08-31 (added pillar cross-references)

## Pillar cross-cuts in this slice

This slice implements parts of the following pillars. Each module in the architecture below maps to a pillar; the pillar is the authority for user stories, scope, and acceptance scenarios.

| Module (in plan below) | Pillar | Pillar spec |
|---|---|---|
| `cases` | P01 | [P01](../../pillars/P01-case-business-object-management/pillar.md) |
| `workflow`, `tasks`, `queues` | P02 | [P02](../../pillars/P02-workflow-tasks-queues/pillar.md) |
| `documents`, `storage` | P03 | [P03](../../pillars/P03-document-content-management/pillar.md) |
| `audit` | P09 | [P09](../../pillars/P09-audit-history-compliance/pillar.md) |
| `configuration` (case types, workflow defs) | P10 | [P10](../../pillars/P10-administration-configuration/pillar.md) |
| `identity`, `authorization` | P11 | [P11](../../pillars/P11-security-identity/pillar.md) |
| `interfaces/http` (REST + OpenAPI), `interfaces/events` (outbox + bus) | P13 | [P13](../../pillars/P13-api-events-extensibility/pillar.md) |

Pillars not represented as modules in this slice (P04, P06, P07, P08, P12, P14) are documented but not built. Their cross-cuts are referenced where the modules they would influence are wired (e.g., P03 ships a quarantine seam even though the full quarantine workflow is a P04/P14 concern).

## Technical Context

**Stack (decided in `/speckit-clarify`):**
- **Backend**: Python 3.12 + FastAPI + SQLAlchemy 2.x (async) + Pydantic v2 + Alembic
- **Frontend**: React 18 + Vite + TypeScript + TanStack Query + React Router
- **Local DB**: PostgreSQL 16 in Docker (development target only; v1 schema is written against SQLAlchemy generic types so it can be ported to Azure SQL with adapter changes — see portability section)
- **Local object store**: filesystem (content-addressable, SHA-256)
- **Local messaging**: in-process dispatcher + durable outbox table
- **Local identity**: seeded development identities (header-based for first slice)
- **Telemetry**: structured JSON logs (structlog) + OpenTelemetry-compatible spans, no exporter
- **Testing**: pytest (backend), vitest + React Testing Library (frontend), Playwright (e2e later)

**Target Azure mappings (not used in v1, defined as contracts):**
- Azure SQL (relational store; **NOT a driver swap** — see Database Portability below)
- Azure Blob Storage (replace `LocalDocumentStore`)
- Azure Service Bus (replace `LocalMessageBus`)
- Microsoft Entra ID (replace `LocalIdentityProvider`)
- Azure Key Vault (replace `LocalSecretProvider`)
- Azure Monitor / Application Insights (add OTLP exporter)

## Architecture

### Module Map (modular monolith)

```
backend/
  app/
    main.py                     # FastAPI app factory, middleware wiring
    config.py                   # Pydantic Settings, env-driven adapter selection
    db.py                       # SQLAlchemy async engine, session factory
    deps.py                     # FastAPI dependencies (auth, db, clock, telemetry)
    
    domain/                     # Pure domain, no framework imports
      tenant.py
      case_type.py              # CaseType + CaseTypeVersion
      case.py                   # Case, CaseStatus, CaseReference
      workflow.py               # WorkflowDefinition + WorkflowVersion
      workflow_instance.py      # WorkflowInstance, Transition, Guard
      task.py
      document.py
      participant.py
      decision.py
      audit.py                  # AuditEvent
      rules.py                  # Declarative JSON condition evaluator
      events.py                 # Domain event base + outbox record
    
    application/                # Use cases, orchestration, transactions
      cases/
        create_case.py
        view_case.py
        edit_case.py
      workflow/
        start_workflow.py
        execute_transition.py
        publish_workflow.py
      tasks/
        complete_task.py
      documents/
        upload_document.py
      decisions/
        record_decision.py
      audit/
        record_event.py
        view_timeline.py
      configuration/
        load_case_type.py
        load_workflow.py
        validate_definition.py
    
    infrastructure/             # Port implementations
      persistence/
        models.py               # SQLAlchemy ORM models
        repositories/           # CaseRepository, TaskRepository, etc.
        migrations/             # Alembic
      storage/
        local_document_store.py
      messaging/
        local_message_bus.py
        outbox_dispatcher.py    # Background worker that drains the outbox
      identity/
        local_identity_provider.py
        current_user.py         # Auth dependency
      secrets/
        local_secret_provider.py
      clock/
        system_clock.py
      telemetry/
        structured_logging.py
        correlation.py          # Correlation/causation IDs per request
    
    interfaces/
      http/
        routers/                # /cases, /tasks, /workflows, /documents, /audit, /admin
        schemas/                # Pydantic request/response models
        errors.py               # Structured error model
        idempotency.py          # Idempotency-Key middleware
        pagination.py
      cli/                      # Typer CLI for synthetic seed, dry-run import
      events/
        local_event_handlers.py # In-process subscribers
  
frontend/
  src/
    api/                        # Typed API client (openapi-typescript)
    cases/                      # Case workspace
    tasks/                      # Task list + task detail
    documents/                  # Document upload + viewer
    audit/                      # Timeline view
    workflow/                   # Transition actions
    shell/                      # Layout, nav, error boundary
    main.tsx
  
docker-compose.yml              # postgres service for local dev
.env.example                    # DOCUMENTS_PATH, DATABASE_URL, etc.
pyproject.toml
package.json
README.md                       # Local startup, synthetic seed, demo flow
```

### Layer Rules (enforced via import linter)

- `domain` imports nothing from FastAPI, SQLAlchemy, or any infrastructure module.
- `application` imports `domain` only. Repositories are passed in as ports (Protocols).
- `infrastructure` implements domain/application-defined Protocols and depends on SQLAlchemy/Pydantic.
- `interfaces/http` depends on `application` (use cases) only — never reaches into `infrastructure` directly.

This is enforced by `import-linter` in CI; violations fail the build (Constitution VII, XV).

### Infrastructure Ports (Protocols in `application/ports.py`)

```python
class CaseRepository(Protocol):
    async def add(self, case: Case) -> None: ...
    async def get(self, case_id: UUID) -> Case | None: ...
    async def list_by_criteria(self, criteria: CaseCriteria) -> Sequence[Case]: ...

class DocumentStore(Protocol):
    async def put(self, content: bytes, *, content_type: str) -> DocumentRef: ...
    async def get(self, ref: DocumentRef) -> bytes: ...

class MessageBus(Protocol):
    async def publish(self, event: DomainEvent) -> None: ...

class IdentityProvider(Protocol):
    def current_user(self) -> UserContext: ...

class SecretProvider(Protocol):
    async def get(self, name: str) -> str: ...

class Clock(Protocol):
    def now(self) -> datetime: ...

class Telemetry(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...
```

The domain knows only the Protocols. Adapters are wired in `app.main` via dependency injection (`app.deps`).

### Domain Model (SQLAlchemy ORM, in `infrastructure/persistence/models.py`)

All tables include `tenant_id` (multi-tenant from day one, Constitution V fail-closed requirement) and `created_at` / `updated_at` (UTC, from injected `Clock`).

- `tenants` (id, name, created_at)
- `users` (id, tenant_id, display_name, roles)
- `case_types` (id, tenant_id, key, name, description, current_version_id, sensitivity_default)
- `case_type_versions` (id, case_type_id, version_number, schema_json, status [draft|published|retired], published_at, published_by)
- `cases` (id, tenant_id, case_type_version_id, reference, status, priority, owner_user_id, owner_queue_id, current_workflow_state, data_json, sensitivity, version_token, created_at, updated_at, closed_at)
- `workflow_definitions` (id, tenant_id, key, name, current_version_id)
- `workflow_versions` (id, workflow_definition_id, version_number, definition_json, status, published_at)
- `workflow_instances` (id, tenant_id, case_id, workflow_version_id, current_state, correlation_id, created_at)
- `workflow_transitions` (id, tenant_id, workflow_instance_id, from_state, to_state, transition_key, actor_id, occurred_at, payload_json)
- `tasks` (id, tenant_id, case_id, type, title, status, assignee_user_id, assignee_queue_id, priority, due_at, idempotency_key, completed_at, completion_outcome)
- `documents` (id, tenant_id, case_id, file_name, content_type, classification, sensitivity, sha256, size, current_version, storage_ref, uploaded_by, uploaded_at)
- `document_versions` (id, document_id, version, sha256, size, storage_ref, uploaded_by, uploaded_at)
- `participants` (id, tenant_id, case_id, role, display_name, external_ref, source, is_primary, added_at, removed_at)
- `decisions` (id, tenant_id, case_id, decision_type, outcome, rationale, evidence_json, made_by, made_at, applicable_workflow_version_id, supersedes_id)
- `audit_events` (id, tenant_id, event_type, occurred_at, actor_id, actor_type, subject_type, subject_id, case_id, correlation_id, causation_id, payload_json, prev_hash, hash)
- `configuration_publications` (id, tenant_id, artifact_type, artifact_id, version_id, action [draft|validate|publish|retire], actor_id, occurred_at, change_summary)
- `outbox` (id, tenant_id, event_type, event_id, payload_json, created_at, dispatched_at, attempt_count, last_error)
- `idempotency_keys` (key, tenant_id, request_hash, response_status, response_body, created_at, expires_at)

Migrations are managed by Alembic with both upgrade and downgrade paths (Constitution XVII).

### Workflow Engine

- **Format**: Custom JSON schema (per `/speckit-clarify` decision). A workflow version is a JSON document:
  ```json
  {
    "states": [{"key": "intake", "is_initial": true}, {"key": "in_review"}, {"key": "decided"}, {"key": "closed", "is_terminal": true}],
    "transitions": [
      {
        "key": "submit_for_review",
        "from": "intake",
        "to": "in_review",
        "guard": {
          "all": [
            {"field": "case.participants", "op": "exists", "min": 1},
            {"field": "case.documents", "op": "exists", "min": 1}
          ]
        },
        "actions": [
          {"type": "create_task", "task_type": "review", "queue": "review_queue"},
          {"type": "start_sla", "sla_key": "review_due", "hours": 48}
        ]
      }
    ]
  }
  ```
- **Engine** (`application/workflow/execute_transition.py`): resolves current state → finds transition → evaluates JSON guard (declarative, no code exec) → applies actions → writes workflow_transitions row + audit event in one transaction → writes outbox row in the same transaction.
- **Validation** (`application/configuration/validate_definition.py`): rejects unreachable states, ambiguous transitions, missing terminal paths, invalid guard operators, unknown queue references, and **non-terminating automatic cycles**. **Human-driven workflow cycles are allowed** (e.g., `intake → in_review → needs_info → in_review`). What is forbidden is a transition that fires automatically (no human action, no external event, no timer precondition) and forms an unconditional loop with no escape guard. The validator distinguishes the two:
  - Cycle with at least one human-driven or system-event transition that can break the loop → allowed.
  - Cycle composed entirely of automatic transitions with no breakable guard → rejected as a potential runaway.
  - States with no possible exit (excluding terminal states) → rejected.
  Blocks publication (FR-015, FR-H scenario).
- **Immutability**: published workflow versions are never UPDATEd; any attempt raises.

### Rule Engine (declarative JSON conditions)

- Operators: `all`, `any`, `not`, `eq`, `ne`, `in`, `gt`, `gte`, `lt`, `lte`, `exists`, `contains`, `regex`.
- Inputs are explicit: each rule declares `inputs: ["case.priority", "case.participants[0].role"]`.
- Evaluator is a pure function in `domain/rules.py` — no I/O, fully testable, deterministic (Constitution XIV, NFR-003).
- Admin rules ship as JSON in `case_type_versions.schema_json.rules[]` and `workflow_versions.definition_json.transitions[].guard`. No code in the admin path.

### Audit (Constitution IV, XVIII)

- Every state-changing use case writes exactly one `audit_events` row in the same DB transaction as the domain change.
- `audit_events.hash` = SHA-256 over (prev_hash + canonical(payload)) for tamper evidence (Constitution XIII.5).
- **Audit events are append-only under all roles, including the migration role.** There is no database role, application path, admin endpoint, script, or migration that mutates or deletes rows in `audit_events`. Schema changes that need to backfill historical event data do so by writing compensating events through the application layer, never by `UPDATE` or `DELETE` on the table. This is enforced at four layers:
  1. **Database**: a trigger rejects UPDATE and DELETE on `audit_events` for every role (including the migration role).
  2. **ORM**: SQLAlchemy event listener that raises on any UPDATE/DELETE of an `AuditEvent` instance.
  3. **Application**: there is no code path that attempts to modify or delete an `AuditEvent`. There is no admin endpoint or script that can do so.
  4. **Migrations**: forward-only schema changes; historical corrections are compensating events written through the application layer, never raw SQL against `audit_events`.
- Corrections are represented by appending a new `audit_events` row of type `AuditCorrectionRecorded` that references the original event id, the reason, and the corrected summary.
- Redaction: payloads reference protected values by id, never by content, by default (Constitution V).

### Identity, Authorization, Tenancy

- `IdentityProvider` returns a `UserContext(user_id, tenant_id, roles, is_system)`. First slice uses header-based dev identities (`X-Dev-User`, `X-Dev-Tenant`) that are only honored when `APP_ENV=local` — fail-closed in any other env (Constitution V, XII.5).
- Authorization (`application/authorization.py`): a small `Policy` registry. `policy.evaluate(action, context)` returns Allow/Deny. Server-side, never UI. Default-deny.
- **Tenant isolation is explicit, not magical.** Every repository method takes `tenant_id` as a required first-class argument; queries always include `WHERE tenant_id = :tenant_id`. Code review must be able to see tenant scoping in every read and write path.
- A SQLAlchemy `before_compile` event listener MAY add a default tenant filter as a **second line of defense**, controlled by a config flag (`tenant_listener_enabled`, default `True` in `app_env=local` and `prod`, default `False` in `test` so tests can exercise cross-tenant assertions). The listener is opt-in and is never the only mechanism: a failing or absent listener must not silently allow cross-tenant access, and a test asserts that a missing context raises. Tests assert tenant isolation on every repository.

### API Surface (FR-030)

Versioned at the URL prefix: `/api/v1/...`.

- `POST /api/v1/cases` — create case (idempotency key required)
- `GET /api/v1/cases/{id}` — case summary
- `PATCH /api/v1/cases/{id}` — edit (requires `If-Match: <version_token>`)
- `POST /api/v1/cases/{id}/transitions` — execute transition
- `GET /api/v1/cases/{id}/timeline` — chronological events
- `POST /api/v1/cases/{id}/documents` — upload (multipart)
- `GET /api/v1/documents/{id}/content` — download (authorization-checked)
- `GET /api/v1/tasks?assignee=me&status=open` — list
- `POST /api/v1/tasks/{id}/complete` — complete (idempotency key required)
- `POST /api/v1/cases/{id}/decisions` — record decision
- `GET /api/v1/admin/case-types`, `POST /api/v1/admin/case-types/{id}/versions`, `POST .../publish`
- `GET /api/v1/admin/workflows`, same lifecycle endpoints
- `GET /api/v1/health` — adapter health, distinguishes local vs Azure adapters

Errors use a single `ErrorResponse` model with `code`, `message`, `details` (safe), and `correlation_id`. Stack traces never reach the client (Constitution V, XIV.3).

### Data Portability (Constitution VI, XII.6)

Database portability is an **architectural objective, not an assumption of dialect equivalence**. The domain layer must not depend on PostgreSQL-specific features, but a future move to Azure SQL is an **adapter and validation effort, not a driver swap**. Expect non-trivial differences in UUID handling, JSON semantics, indexing, identity/sequence behavior, datetime/timezone semantics, transaction isolation, SQL syntax, and migration tooling.

Rules:

- **All domain column types use SQLAlchemy generic types**:
  - `sqlalchemy.Uuid` (not dialect-specific `UUID`)
  - `sqlalchemy.JSON` (not PostgreSQL `JSONB`)
  - `sqlalchemy.DateTime(timezone=True)` (timezone-aware)
  - `sqlalchemy.Text`, `sqlalchemy.String(length)`, `sqlalchemy.Integer`, `sqlalchemy.Boolean`
  - `sqlalchemy.ForeignKey`, `sqlalchemy.Index` (dialect-neutral)
- **No PostgreSQL operators in domain code**. `JSONB` containment (`@>`), GIN indexes, `ILIKE`, `tsvector`, `gen_random_uuid()`, array types, range types, etc. are forbidden in the `domain` and `application` layers. If a query seems to need one, the need belongs in the repository port and must be expressed portably (or deferred behind a feature flag).
- **No PostgreSQL-specific SQL in Alembic migrations**. Migrations use SQLAlchemy operations or portable SQL; raw dialect SQL is allowed only with a portability comment.
- **Search** starts with portable `LIKE` / case-insensitive equality via the database's default collation. A real search engine (PostgreSQL FTS, Azure Cognitive Search) is an adapter concern, never a domain concern.
- **Document content** addressed by SHA-256, not by provider-specific key. Document metadata stored as `sqlalchemy.JSON` (portable).

## Project Structure & Conventions

- **Python**: `pyproject.toml` (PEP 621), `ruff` for lint+format, `mypy --strict` for `app/domain` and `app/application`, `pytest` with `pytest-asyncio`. Coverage gate ≥ 80% for `domain` and `application`. `.python-version` pins 3.12.
- **TypeScript**: strict mode, `eslint` + `prettier`, `vitest`, generated types via `openapi-typescript` from the FastAPI OpenAPI schema. `.nvmrc` pins Node 20.
- **Commit messages**: Conventional Commits.
- **Branching**: `001-case-management-platform` for this feature; future slices branch from `main`.

## Testing Strategy (Constitution XVI)

- **Unit**: every `domain` entity has property-based invariants tests; rule evaluator has exhaustive truth-table tests for every operator.
- **Application**: each use case has a test with an in-memory fake repository; covers success, validation failure, authorization denial, idempotent retry, stale concurrency token, and dependency failure (storage unavailable).
- **Workflow**: scenario-based tests for the seed workflow, including Scenario D (execute transition) and Scenario E (explain blocked transition) from the spec.
- **HTTP**: FastAPI test client asserts the documented error model and idempotency behavior.
- **E2E (later)**: Playwright runs the documented local demo flow against `docker compose up`.
- **Security tests** (Phase 3+): tenant-isolation tests, authorization tests, file-handling tests, audit-integrity tests.

## Local Development Contract (Constitution VI, XII.4)

- `docker compose up -d` starts PostgreSQL on `localhost:5432`.
- `uv run python -m app.cli seed` loads synthetic case type, workflow, and one demo case.
- `uv run uvicorn app.main:app --reload` starts the API.
- `pnpm dev` starts the React app on `http://localhost:5173` proxying `/api` to the backend.
- README includes a one-command smoke test that creates a case via the API and prints the reference.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Alembic + async SQLAlchemy ergonomics | Use `alembic init --template async`; keep migrations small and reversible. |
| Declarative rule engine becomes a Turing tarpit | Hard cap on guard nesting depth (validated); whitelist of operators only. |
| Outbox dispatcher doubles as a "second source of truth" | Treat outbox as the ONLY source of integration events; never publish outside it. |
| Synthetic data accidentally resembles real PHI | Use clearly marked fake domains (`example.test`), realistic-looking but obviously fake names, no DOBs in fixtures. |
| Multi-tenancy drift (query forgets `tenant_id`) | SQLAlchemy event listener that injects `tenant_id` from session context; tests assert isolation. |
| Audit hash chain breaks on partial failure | Hash is computed at commit time only; if commit fails, no row, no hash. |

## Out of Scope (first slice)

- Microservices, queues with broker, Azure adapters, Entra integration, Key Vault, real-time notifications, AI features, OnBase/Panzura connectors, multi-region, autoscaling, rich queue/config authoring UIs, mobile clients, e-signature, e-fax, real email/SMS, real document scanning.

## Open Decisions Deferred to Phase 3+

- **Standalone rules CRUD** (deferred to a later spec) — for v1, rules are embedded inside workflow version JSON. A standalone `/admin/rules` lifecycle endpoint is a separate future spec.
- Audit hash chain vs signed batches (start with chained SHA-256, evaluate signing later).
- Full-text search engine (start with portable `LIKE`, evaluate Azure Cognitive Search later).
- Notification channels (start with in-process log sink, evaluate SES/ACS later).
