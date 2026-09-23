# Technical Specification — Case Management Platform

**Status**: Living document — single source of truth for stack, data model, and module interfaces
**Owner**: Platform team
**Constitution**: [../.specify/memory/constitution.md](../.specify/memory/constitution.md) — highest authority
**Plan**: [../specs/001-case-management-platform/first-slice/plan.md](../specs/001-case-management-platform/first-slice/plan.md)
**Architecture**: [../specs/001-case-management-platform/architecture.md](../specs/001-case-management-platform/architecture.md)
**Last updated**: 2026-08-31

This document is the technical baseline for the first substantial slice. It exists so agents do not invent stack decisions. Stack choices are locked in `/speckit-clarify` (see `specs/001-case-management-platform/first-slice/spec.md §Resolved Decisions`). Constitution prevails on any conflict (AGENTS.md §6).

---

## 1. Stack

### 1.1 Backend

| Concern | Choice | Pin | Why |
|---|---|---|---|
| Runtime | Python | `3.12` (`.python-version`) | Constitution / spec.md §Resolved Decisions |
| Web framework | FastAPI | latest stable | async-native, OpenAPI generation |
| ORM | SQLAlchemy 2.x (async) | `^2.0` | portable generic types; explicit tenant scoping |
| Validation / settings | Pydantic v2 + pydantic-settings | `^2.0` | typed schemas, settings, errors |
| Migrations | Alembic | latest stable | reversible migrations; both `upgrade` and `downgrade` |
| Logging | structlog | latest stable | JSON logs; correlation/causation binding |
| CLI | Typer | latest stable | `app/cli/seed.py`, `verify-audit` |
| ID generation | `uuid.uuid7` (or `uuid4`) | stdlib | monotonic-ish IDs for events; UUIDs throughout |
| Test | pytest + pytest-asyncio + pytest-cov | latest stable | gate: ≥80% on `domain` + `application` (AGENTS.md §9) |
| Lint | ruff (line length 100, `target-version=py312`) | latest stable | single tool for lint+format (AGENTS.md §4.R6) |
| Types | mypy --strict on `app/domain` and `app/application` | latest stable | AGENTS.md §4.R6 |
| Layer rules | import-linter | latest stable | enforces layer boundaries (AGENTS.md §4.R10) |
| DB driver | asyncpg | latest stable | Postgres async |

### 1.2 Frontend

| Concern | Choice | Pin |
|---|---|---|
| Runtime | Node | `20` (`.nvmrc`) |
| Framework | React 18 | `^18` |
| Build | Vite | latest stable |
| Language | TypeScript strict | `^5` |
| Data | TanStack Query | `^5` |
| Routing | React Router | `^6` |
| Types | openapi-typescript | latest stable (generated from `/openapi.json`) |
| Lint / format | eslint + prettier | latest stable |
| Tests | vitest + @testing-library/react | latest stable |

E2E (Playwright) is deferred past first slice (AGENTS.md §5).

### 1.3 Local infrastructure (CLI only; Docker Desktop unavailable)

```text
docker CLI                  present (verify with `docker info` before use)
docker compose               present (verify with `docker compose version`)
PostgreSQL 16                via `docker compose up -d` → cmp-postgres on :5432
content-addressable FS       under DOCUMENTS_PATH (./documents by default)
in-process MessageBus        + durable outbox table
X-Dev-User / X-Dev-Tenant    headers, only honored when APP_ENV=local
SQLite (test profile)        APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db'
```

WSL registry corruption caveat (AGENTS.md §3, §7): a task must verify with `docker info` and `docker compose ps` before assuming Postgres is up, and must fall back to the sqlite profile if `docker compose up -d` errors. Tasks must not block solely on container-start failures.

### 1.4 Toolchain

- `.python-version` → `3.12`
- `.nvmrc` → `20`
- Conventional Commits; branch naming `NNN-short-name` (AGENTS.md §11)

---

## 2. Data Model (Portable Core)

> **Rules (plan.md:289):** All column types are SQLAlchemy **generic** types. No `JSONB`, no `GIN`, no `ILIKE`, no `tsvector`, no `gen_random_uuid()`, no array/range types. Search starts with portable `LIKE`. Migrations must be reversible and portable.

### 2.1 Type mapping

| Domain concept | SQLAlchemy generic type | Notes |
|---|---|---|
| Identifier (UUID) | `sqlalchemy.Uuid` | not dialect-specific `UUID` |
| Structured payload | `sqlalchemy.JSON` | not `JSONB` |
| Timestamp | `sqlalchemy.DateTime(timezone=True)` | always UTC; from injected `Clock` |
| String | `sqlalchemy.String(length)` / `sqlalchemy.Text` | explicit length where bounded |
| Integer | `sqlalchemy.Integer` | identity / counters |
| Boolean | `sqlalchemy.Boolean` | |
| FK / Index | `sqlalchemy.ForeignKey` / `sqlalchemy.Index` | dialect-neutral |

If a query needs a Postgres-only operator (`@>`, `ILIKE`, `tsvector`), the need belongs in a **repository port** and must be expressed portably (or deferred behind a feature flag).

### 2.2 Canonical tables (first slice)

> Mirrors plan.md §16.1 and `infrastructure/persistence/models.py`. Every table includes `tenant_id` and `created_at`/`updated_at` (UTC from `Clock`).

| Table | Columns (compact) | Notes |
|---|---|---|
| `tenants` | `id, key, name, created_at` | top of tenancy |
| `users` | `id, tenant_id FK, key, display_name, roles JSON, is_system, created_at, updated_at` | `uq_users_tenant_key` |
| `case_types` | `id, tenant_id FK, key, name, description, current_version_id, created_at, updated_at` | `uq_case_types_tenant_key` |
| `case_type_versions` | `id, tenant_id FK, case_type_id FK, version_number, schema_json JSON, status [draft\|published\|retired], published_at, published_by, created_at` | `uq_case_type_versions_num` |
| `cases` | `id, tenant_id FK, case_type_version_id FK, reference, status, priority, owner_user_id, owner_queue_id, current_workflow_state, data_json JSON, sensitivity, version_token, created_at, updated_at, created_by, closed_at` | `uq_cases_tenant_ref`; `ix_cases_tenant_status` |
| `workflow_definitions` | `id, tenant_id FK, key, name, current_version_id, created_at, updated_at` | |
| `workflow_versions` | `id, workflow_definition_id FK, version_number, definition_json JSON, status, published_at, created_at, updated_at` | |
| `workflow_instances` | `id, tenant_id FK, case_id FK, workflow_version_id FK, current_state, correlation_id, created_at` | |
| `workflow_transitions` | `id, tenant_id FK, workflow_instance_id FK, from_state, to_state, transition_key, actor_id, occurred_at, payload_json` | |
| `tasks` | `id, tenant_id FK, case_id FK, type, title, status [open\|completed\|cancelled], assignee_user_id, assignee_queue_id, priority, due_at, idempotency_key, completed_at, completion_outcome, created_at, updated_at` | |
| `documents` | `id, tenant_id FK, case_id FK, file_name, content_type, classification, sensitivity, sha256, size, current_version, storage_ref, uploaded_by, uploaded_at, created_at, updated_at` | content held behind `DocumentStore` |
| `document_versions` | `id, document_id FK, version, sha256, size, storage_ref, uploaded_by, uploaded_at` | append-only |
| `participants` | `id, tenant_id FK, case_id FK, role, display_name, external_ref, source, is_primary, added_at, removed_at` | |
| `decisions` | `id, tenant_id FK, case_id FK, decision_type, outcome, rationale, evidence_json, made_by, made_at, applicable_workflow_version_id, supersedes_id` | |
| `audit_events` | `id, tenant_id FK, event_type, occurred_at, actor_id, actor_type, subject_type, subject_id, case_id, correlation_id, causation_id, payload_json, prev_hash, hash` | **append-only** — DB trigger + ORM listener + no app mutation path (plan.md:252) |
| `configuration_publications` | `id, tenant_id FK, artifact_type, artifact_id, version_id, action [draft\|validate\|publish\|retire], actor_id, occurred_at, change_summary` | |
| `outbox` | `id, tenant_id FK, event_type, event_id, payload_json, created_at, dispatched_at, attempt_count, last_error` | drained by `outbox_dispatcher` |
| `idempotency_keys` | `key, tenant_id, request_hash, response_status, response_body, created_at, expires_at` | replay returns cached response |

### 2.3 Migration conventions

- `alembic revision --autogenerate` produces both `upgrade()` and `downgrade()`.
- Raw dialect SQL is allowed only with a portability comment block.
- Tenant FK columns are always indexed.
- Naming convention (`app/db.py:22`): `pk_*`, `fk_*`, `ix_*`, `uq_*`.
- Reversibility is part of the Definition of Done (Constitution XVII; AGENTS.md §14).

---

## 3. Module / Port Interfaces

> Pseudocode only. All ports live as `typing.Protocol` in `app/domain/ports.py` (or `app/application/ports.py`). Adapters implement them in `app/infrastructure/`. The domain never imports FastAPI, SQLAlchemy, or any infrastructure module.

### 3.1 Persistence ports

```python
class CaseRepository(Protocol):
    async def add(self, case: Case) -> None: ...
    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None: ...
    async def get_by_reference(
        self, tenant_id: UUID, reference: CaseReference
    ) -> Case | None: ...
    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Case]: ...
    async def next_reference(
        self, tenant_id: UUID, *, now: datetime
    ) -> CaseReference: ...


class CaseTypeRepository(Protocol):
    async def get(self, tenant_id: UUID, case_type_id: UUID) -> CaseType | None: ...
    async def get_by_key(self, tenant_id: UUID, key: str) -> CaseType | None: ...


class CaseTypeVersionRepository(Protocol):
    async def get(
        self, tenant_id: UUID, case_type_version_id: UUID
    ) -> CaseTypeVersion | None: ...
    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None: ...
    async def add(self, version: CaseTypeVersion) -> None: ...


# WorkflowInstanceRepository, TaskRepository, DocumentRepository,
# ParticipantRepository, DecisionRepository, AuditEventStore,
# ConfigurationRepository, ImportJobRepository — same shape
```

### 3.2 Outbox + audit ports

```python
class OutboxWriter(Protocol):
    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None: ...


class AuditEventStore(Protocol):
    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID | None,
        subject_type: str,
        subject_id: UUID | None,
        case_id: UUID | None,
        correlation_id: UUID | None,
        causation_id: UUID | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None: ...
    # Verify helper:
    async def verify_chain(self, tenant_id: UUID) -> bool: ...


# hash = SHA-256(prev_hash + canonical(payload))
# canonical(payload) = JSON with stable key order
```

### 3.3 Storage / messaging / identity / secrets / telemetry / clock

```python
class DocumentStore(Protocol):
    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str: ...
    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes: ...


class MessageBus(Protocol):
    async def publish(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None: ...


class IdentityProvider(Protocol):
    def current_user(self) -> UserContext: ...


class SecretProvider(Protocol):
    async def get(self, name: str) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class Telemetry(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...


class UserContext:
    user_id: UUID
    tenant_id: UUID
    roles: tuple[str, ...]
    is_system: bool
```

### 3.4 AI seam (bounded assistance only — Constitution XIV)

```python
class WorkflowSuggestionPort(Protocol):
    """FM-034 / P10 — suggestions only; never authoritative."""
    async def suggest_transition_targets(
        self, *, case: CaseSnapshot, k: int
    ) -> tuple[Recommendation, ...]: ...


class ExtractionPort(Protocol):
    """P05 — suggestion of field values; never replaces deterministic validation."""
    async def propose_fields(
        self, *, document_text: str, schema: CaseTypeSchema
    ) -> tuple[FieldProposal, ...]: ...
```

Adapters are stubs today; Azure OpenAI adapter lands later (P11-US12 seam).

---

## 4. Application Use Cases — Pseudocode

Use cases live in `app/application/<module>/<verb>.py` as `handle(cmd, *, repo-deps, clock)` functions. They orchestrate, validate, and write — never persist directly.

### 4.1 `application/cases/create_case.py`

```python
@dataclass(frozen=True)
class CreateCaseCommand:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_type_key: str
    title: str
    description: str
    data: dict[str, Any]
    priority: str = "normal"
    idempotency_key: str | None = None  # required at HTTP layer


async def handle(
    cmd: CreateCaseCommand,
    *,
    case_repo: CaseRepository,
    case_type_repo: CaseTypeRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    policy: Policy,
    clock: Clock,
) -> Case:
    policy.evaluate("case.create", actor=cmd.actor_id, roles=cmd.actor_roles)
    case_type = await case_type_repo.get_by_key(cmd.tenant_id, cmd.case_type_key) \
        or raise NotFound("case type")
    version = await case_type_version_repo.get(
        cmd.tenant_id, case_type.current_version_id
    ) or raise NotFound("case type version")
    now = clock.now()
    reference = await case_repo.next_reference(cmd.tenant_id, now=now)
    case = Case.create(
        tenant_id=cmd.tenant_id,
        reference=reference,
        case_type_version=version,
        title=cmd.title,
        description=cmd.description,
        data=cmd.data,
        priority=cmd.priority,
        created_by=cmd.actor_id,
        now=now,
    )
    await case_repo.add(case)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type="CaseCreated",
        actor_id=cmd.actor_id,
        subject_type="case",
        subject_id=case.id,
        case_id=case.id,
        correlation_id=None, causation_id=None,
        payload={
            "reference": case.reference.value,
            "case_type_key": cmd.case_type_key,
            "case_type_version_id": str(version.id),
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type="CaseCreated",
        event_id=uuid4(),
        payload={"case_id": str(case.id), "reference": case.reference.value},
    )
    return case
```

The router calls this inside one DB transaction so audit + outbox commit atomically with the domain change (Constitution XIII.5; SC-002).

### 4.2 `application/workflow/execute_transition.py`

```python
async def handle(cmd, *, workflow_repo, audit, outbox, policy, clock):
    policy.evaluate("workflow.transition", actor=cmd.actor_id)
    instance = await workflow_repo.get(cmd.tenant_id, cmd.workflow_instance_id) \
        or raise NotFound("workflow instance")
    version = await workflow_repo.get_version(
        cmd.tenant_id, instance.workflow_version_id
    ) or raise NotFound("workflow version")
    transition = resolve_transition(version.definition, instance.current_state, cmd.key) \
        or raise GuardFailed("unknown or invalid transition", transition_key=cmd.key)
    if not rules.evaluate(transition.guard, snapshot(instance, cmd.case)):
        raise GuardFailed("guard failed", requirements=transition.guard)
    new_instance = instance.apply(transition.to, occurred_at=clock.now())
    await workflow_repo.replace(instance, new_instance)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type="WorkflowTransitioned",
        actor_id=cmd.actor_id,
        subject_type="workflow_instance",
        subject_id=new_instance.id,
        case_id=new_instance.case_id,
        payload={
            "from": instance.current_state, "to": new_instance.current_state,
            "transition_key": transition.key, "workflow_version_id": str(version.id),
        },
        occurred_at=clock.now(),
    )
    await outbox.enqueue(...)  # one row per emitted event
    for action in transition.actions:
        await execute_action(action, snapshot(new_instance, cmd.case), ...)
```

### 4.3 `application/documents/upload_document.py`

```python
async def handle(cmd, *, doc_repo, store: DocumentStore, audit, outbox, policy, clock):
    policy.evaluate("document.upload", actor=cmd.actor_id)
    sha = sha256_hex(cmd.content)
    validate_classification(cmd.classification)
    if cmd.size > settings.max_document_size:
        raise ValidationFailed("document exceeds configured size limit")
    storage_ref = await store.put(
        tenant_id=cmd.tenant_id, content=cmd.content,
        content_type=cmd.content_type, sha256=sha,
    )
    document = Document.create(..., sha256=sha, storage_ref=storage_ref, ...)
    await doc_repo.add(document)
    await audit.append(event_type="DocumentAdded", payload={...})
    await outbox.enqueue(event_type="DocumentAdded", ...)
```

---

## 5. Authorization & Tenant Isolation

- **Authorization** (`application/authorization.py`): a small `Policy` registry; `policy.evaluate(action, ctx) -> Allow | Deny`. Default-deny. Server-side, never UI.
- **Tenant isolation** is **explicit, not magical**: every repository method takes `tenant_id` as a required first-class argument; queries always include `WHERE tenant_id = :tenant_id` (plan.md:265; AGENTS.md §4.R10).
- A SQLAlchemy `before_compile` listener MAY add a default tenant filter as defense-in-depth, controlled by `config.TENANT_LISTENER_ENABLED` (default `True` in `local`/`prod`, default `False` in `test`). It is **never the sole mechanism**: a missing context must raise, not silently leak.
- Tenant-isolation tests assert two-tenant isolation on every repository (AGENTS.md §5, §9).

---

## 6. Identity & Dev Headers

```python
# app/deps.py (pseudocode)
def get_current_user(request: Request) -> UserContext:
    settings = get_settings()
    if settings.app_env == "local":
        uid = request.headers.get("x-dev-user")
        tid = request.headers.get("x-dev-tenant")
        if not uid or not tid:
            raise HTTPException(401, "dev identity required")
        return UserContext(user_id=UUID(uid), tenant_id=UUID(tid),
                           roles=resolve_roles(uid), is_system=False)
    # Anything else: real token validation (Entra later)
    raise HTTPException(401, "unauthorized")
```

`SYNTHETIC_ENVIRONMENT=1` is logged on startup (`app/main.py:95`) and shown in the UI banner. Production (`shared`, `prod`) rejects `X-Dev-User` / `X-Dev-tenant` with 401 — fail-closed (Constitution V).

---

## 7. API Surface (REST + OpenAPI)

- URL prefix: `/api/v1/...` (plan.md:268).
- Mutations require `Idempotency-Key`; replays return the cached response.
- Edits require `If-Match: <version_token>`; stale tokens return 409.
- `Page[T] { items, next_cursor, limit }` default 50, max 200 (plan.md:284).
- `ErrorResponse { code, message, details, correlation_id }`; never leaks SQL/stack/secrets.

Endpoints (first slice):

```text
POST   /api/v1/cases
GET    /api/v1/cases/{id}
GET    /api/v1/cases
PATCH  /api/v1/cases/{id}                          # If-Match
POST   /api/v1/cases/{id}/transitions
GET    /api/v1/cases/{id}/timeline
POST   /api/v1/cases/{id}/documents                # multipart
GET    /api/v1/documents/{id}/content              # Content-Disposition
GET    /api/v1/tasks?assignee=me&status=open
POST   /api/v1/tasks/{id}/complete                 # Idempotency-Key
POST   /api/v1/cases/{id}/decisions
GET    /api/v1/admin/case-types
POST   /api/v1/admin/case-types/{id}/versions
POST   /api/v1/admin/case-types/{id}/versions/{n}/publish
GET    /api/v1/admin/workflows
POST   /api/v1/admin/workflows/{id}/versions/{n}/simulate
GET    /api/v1/health                              # adapters + synthetic flag
```

Frontend types are **generated**:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o frontend/src/api/types.gen.ts
```

---

## 8. Workflow Engine Contract

A workflow version is JSON (plan.md:211):

```jsonc
{
  "states": [
    {"key": "intake", "is_initial": true},
    {"key": "in_review"},
    {"key": "decided"},
    {"key": "closed", "is_terminal": true}
  ],
  "transitions": [
    {
      "key": "submit_for_review",
      "from": "intake", "to": "in_review",
      "guard": {
        "all": [
          {"field": "case.participants", "op": "exists", "min": 1},
          {"field": "case.documents", "op": "exists", "min": 1}
        ]
      },
      "actions": [
        {"type": "create_task", "task_type": "review", "queue": "review_queue"},
        {"type": "start_sla", "sla_key": "review_due", "hours": 48}
      ],
      "human_driven": true
    }
  ]
}
```

Validators reject at publish: unreachable states, ambiguous transitions, no-exit states, **automatic runaway cycles**. Human cycles (with a `human_driven`/timer break) are allowed (plan.md:235).

Rule operators: `all`, `any`, `not`, `eq`, `ne`, `in`, `gt`, `gte`, `lt`, `lte`, `exists`, `contains`, `regex`. Nesting depth capped at publish. Evaluator is pure (no I/O) — `domain/rules.py` (plan.md:244).

---

## 9. Audit & Hash Chain

- Every state-changing use case writes one `audit_events` row in the **same DB transaction** as the domain change.
- `hash = SHA-256(prev_hash + canonical(payload))`; `prev_hash` is the latest hash for the tenant (plan.md:252).
- Append-only at **four layers**:
  1. **Database**: trigger rejects UPDATE / DELETE on `audit_events` for every role including the migration role.
  2. **ORM**: SQLAlchemy event listener that raises on UPDATE/DELETE.
  3. **Application**: no path attempts mutation/deletion; no admin endpoint does.
  4. **Migrations**: forward-only schema changes; corrections are compensating events (`AuditCorrectionRecorded`) through the application, never raw SQL against `audit_events`.
- `verify-audit` CLI replays the chain per tenant.
- Payloads reference protected values by id, never by content (Constitution V).

---

## 10. Outbox & Asynchrony

- The application writes `outbox` rows in the same transaction as the domain change (plan.md:206).
- `outbox_dispatcher` (`infrastructure/messaging/outbox_dispatcher.py`) drains pending rows through the configured `MessageBus` adapter; backoff + dead-letter after N attempts.
- Outbox is the **only** source of integration events. No code path publishes outside the outbox.

---

## 11. Configuration & Synthetic Data

- `app/cli/seed.py` (Typer) creates a tenant, three users (`intake`, `reviewer`, `supervisor`), `CaseType ServiceRequest`, the seed workflow, and a demo case.
- All fixtures use `example.test`, obviously fake names, no DOBs, no PHI.
- `SYNTHETIC_ENVIRONMENT=1` is logged at startup and shown in the UI.

---

## 12. Quality Gates (must pass before task-done)

Per AGENTS.md §9 and §15:

```bash
# Code quality
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app/domain app/application
uv run lint-imports verify

# Tests (sqlite fallback — no Docker required)
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q \
    --cov=app/domain --cov=app/application --cov-report=term-missing

# Frontend
pnpm typecheck
pnpm build
```

- Coverage ≥ 80% on `domain` + `application` (AGENTS.md §9).
- No file > 1000 lines; no function > 50 lines without justification (AGENTS.md §4.R2).
- Every new API has OpenAPI entry + error model + idempotency/concurrency as required.
- Tenant-isolation test with two tenants for every repository/query.
- `grep -R example.test` must pass for fixtures; no PHI patterns.

---

## 13. Reference Index

| Topic | Source |
|---|---|
| Pillar definitions | `specs/pillars/PXX-*/pillar.md` |
| Roadmap | `specs/pillars/00-product-roadmap.md` |
| Feature map | `specs/pillars/01-deduplicated-feature-map.md` |
| Slice spec / plan / tasks | `specs/001-case-management-platform/first-slice/{spec,plan,tasks}.md` |
| Architecture (Mermaid) | `specs/001-case-management-platform/architecture.md` |
| Agent contract | `AGENTS.md` |
| Constitution | `.specify/memory/constitution.md` |
| Integration seams | `docs/integrations/seams.md` (create if missing) |
| Decisions | `docs/decisions/ADR-NNN-*.md` |