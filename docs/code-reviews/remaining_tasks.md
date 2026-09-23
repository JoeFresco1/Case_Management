# Remaining Tasks — Case Management Platform

**Source**: `specs/pillars/01-deduplicated-feature-map.md` (H1–H3 features)  
**Status**: First slice (001-case-management-platform, Phases 0–3) complete. Remaining = H1/H2/H3/Gated features.

---

## 1. Remaining Feature Catalog (by Horizon)

### H1 — Next (Directly usable after first slice)

| ID | Feature | Primary Pillar | Depends On | Status |
|----|---------|----------------|------------|--------|
| **FM-048** | Role and permission administration | P11 | FM-002 ✅ | **DONE** (2026-09-10: router + use cases + tests green; ADR-027) |
| **FM-011** | Sensitive case and field controls | P11 | FM-002 ✅, FM-008 ✅, FM-005 ✅ | **DONE** (2026-09-10: redactor + policy + tests green) |
| **FM-021** | Configuration governance and publication history | P10 | FM-008 ✅, FM-013 ✅ | **DONE** (2026-09-10: governance use cases + admin router + tests green) |
| **FM-031** | Authorized structured case search | P06 | FM-002 ✅, FM-006 ✅, FM-008 ✅, FM-011 ✅ | **DONE** (2026-09-11: GET+POST search + redacted snippets verified, suite green; unblocks FM-032/033) |
| **FM-016** | Queue administration and worker distribution | P02 | FM-014 ✅ | **DONE** (2026-09-10: SqlQueueRepository CRUD + migration 0020 + router wiring + catalog events; ADR-030) |
| **FM-017** | Approval transitions and separation of duties | P02 | FM-014 ✅, FM-016 ✅ | **DONE** (2026-09-11: approval_required + eligible_approver_roles in WorkflowTransition; SoD enforcement in execute_transition + preflight; validation in validate_workflow_definition; 16 tests green) |
| **FM-018** | SLA deadlines and escalation | P02 | FM-014 ✅ (basic seam in H0) | **DONE** (2026-09-11: SlaClock lifecycle + scan_once/scan_warning_once + escalation actions (create_task) + workflow SLA validation; 55 tests green) |
| **FM-052** | Failed-work and outbox operations | P13 | FM-014 ✅, FM-003 ✅ | **DONE** (2026-09-10: admin outbox API + dispatcher + tests green) |
| **FM-055** | Versioned event catalog and reliable delivery | P13 | FM-014 ✅, FM-003 ✅ | **DONE** (2026-09-10: catalog + 7 queue/task events registered; ADR-030) |
| **FM-026** | Import mapping, dry run, and reconciliation | P14 | FM-005 ✅, FM-022 ✅ | **DONE** (2026-09-11: admin CRUD + mapping_id support + GET /imports/{id}; tests green) |
| **FM-027** | Ingestion-channel governance and quarantine | P04 | FM-026 ✅ | **DONE** (2026-09-11: GET /imports/{job_id} + list imports; tests green) |
| **FM-039** | Legal-hold lifecycle | P08 | FM-003 ✅ (refusal seam in H0) | **DONE** (2026-09-11: LegalHold aggregate + place/release + SQL repo + hold scanner; tests green) |
| **FM-036** | Versioned conditional case forms | P07 | FM-005 ✅, FM-008 ✅, FM-021 ✅ | **DONE** (2026-09-11: FormBinding.retire() + mark_retired() + admin endpoints + tests green) |

### H2 — Expansion (After core workflows stable)

> **Status 2026-09-11: all H2 rows DONE.** Two needed implementation
> (FM-009, FM-035); the rest verified operable (routes mounted, SQL
> providers resolve, per-feature suites green, ~1500 tests total).

| ID | Feature | Primary Pillar | Depends On | Status |
|----|---------|----------------|------------|--------|
| **FM-009** | Tenant-configurable case references | P01 | FM-005 ✅ | **DONE** (SQL formats + template issuance + 200-char refs; ADR-032) |
| **FM-010** | Case relationships and restructuring | P01 | FM-005 ✅, FM-009 ✅ | **DONE** (relate/split/merge wired; split passes type key; suite green) |
| **FM-025** | Document protection, redaction, and locking | P03 | FM-022 ✅, FM-023 ✅ | **DONE** (redact/lock endpoints + tests green) |
| **FM-032** | Saved and auditable searches | P06 | FM-031 ✅ | **DONE** (save/list/run + query-shape audit; suite green) |
| **FM-033** | Authorized in-case document search | P06 | FM-022 ✅, FM-025 ✅, FM-031 ✅ | **DONE** (endpoint + redacted snippets; suite green) |
| **FM-035** | Bounded break-glass access | P11 | FM-002 ✅, FM-011 ✅ | **DONE** (SQL grants + read/search enforcement + flagging; ADR-031) |
| **FM-040** | Versioned retention and reviewed disposition | P08 | FM-003 ✅, FM-021 ✅, FM-023 ✅, FM-039 (partial, refusal seam + holds place endpoint) | **DONE** (policy/disposition/archival/freeze/orphan routes + tests green) |
| **FM-044** | Audit export and corrective annotations | P09 | FM-003 ✅, FM-043 ✅ | **DONE** (`/audit/export`, corrections, signed batches; suite green) |
| **FM-045** | Access-decision audit and reporting | P09 | FM-002 ✅, FM-011 ✅ | **DONE** (`/audit/access-decisions` report; suite green) |
| **FM-051** | Operational case and task reports | P12 | Stable read models ✅ | **DONE** (open-case/overdue-task/custom-report/dashboard routes green) |

### H3 — Advanced (Deferred sophistication)

> **Status 2026-09-11: all non-Gated H3 rows DONE.** Four needed
> implementation (FM-034, FM-038, FM-056, FM-057); the rest verified
> operable (routes mounted, suites green). Five rows stay Gated by
> design (mechanics operable, gated switches off — see table).

| ID | Feature | Primary Pillar | Depends On | Status |
|----|---------|----------------|------------|--------|
| **FM-012** | Derived fields and durable field identity | P05 | FM-008 ✅, FM-011 ✅ | **DONE** (computed at intake/edit; suite green) |
| **FM-019** | Advanced workflow composition | P02 | FM-013 ✅, FM-017 (READY) | **DONE** (subprocess/parallel/join in domain + validator; suite green) |
| **FM-020** | Visual workflow designer | P10 | FM-013 ✅ | **DONE** (VisualDesigner UI + same definition contract; backend serves validated definitions) |
| **FM-028** | Inbound email capture | P04 | FM-027 (H1-READY) | **DONE** (capture endpoint + channel flow; suite green) |
| **FM-029** | Scan capture and safety quarantine | P04 | FM-027 (H1-READY, Gated device) | **DONE mechanics, GATED device** (quarantine/release tested; physical scanner not connected) |
| **FM-030** | Inbound webhook commands | P04 | FM-022 ✅ | **DONE** (validated commands + provenance; suite green) |
| **FM-034** | Explainable semantic search | P06 | FM-031 ✅, FM-033 ✅ | **DONE** (`POST /search/semantic`, auth-first ranking; ADR-033) |
| **FM-037** | Visual form designer | P07 | FM-036 (H1-READY) | **DONE** (VisualFormDesigner UI + designer contract tests; same versioned format) |
| **FM-038** | Advanced form execution | P07 | FM-036 (H1-READY) | **DONE** (SQL sessions + steps persistence + commits + If-Match; ADR-033) |
| **FM-041** | Archival and disposition automation | P08 | FM-040 ✅ | **DONE** (archive/unfreeze/freeze/orphan routes; suite green) |
| **FM-042** | Defensible-deletion evidence report | P08 | FM-040 ✅ | **DONE** (evidence report endpoint; suite green) |
| **FM-046** | Externally verifiable audit evidence | P09 | FM-003 ✅, FM-044 ✅ (Gated SIEM) | **DONE mechanics, GATED stream** (signed batches operable; `audit.stream` ungranted per ADR-009) |
| **FM-047** | Versioned extension and event-type registration | P13 | FM-055 ✅ | **DONE** (registration endpoints; suite green) |
| **FM-049** | Session and trusted-client controls | P11 | FM-048 ✅ (Gated) | **DONE mechanics, GATED switches** (idle/lockout checks tested; policy/cert publish ungranted per ADR-011; store in-memory) |
| **FM-050** | Human-authoritative AI action boundary | P11 | FM-002 ✅ | **DONE** (rank-only seam, no LLM in transitions/auth; recommendation logged separately per ADR-012) |
| **FM-053** | Governed report export and custom definitions | P12 | FM-051 ✅ | **DONE** (custom reports + governed export; suite green) |
| **FM-054** | Fresh operational dashboards | P12 | FM-051 ✅ | **DONE** (dashboard endpoints; suite green) |
| **FM-056** | Authenticated outbound webhooks | P13 | FM-055 ✅ | **DONE** (signed deliveries + background dispatch loop wired; ADR-033) |
| **FM-057** | Governed GraphQL query surface | P13 | FM-004 ✅ | **DONE** (`POST /api/v1/graphql`, policy-enforced resolvers; ADR-033) |
| **FM-058** | Approved connector lifecycle | P14 | FM-058 (Gated self) | **DONE mechanics, GATED named** (synthetic lifecycle tested; `CONNECTOR_NAMED_IMPLEMENT` ungranted) |
| **FM-059** | Source coexistence and migration state | P14 | FM-026 (H1-READY, Gated sync) | **DONE mechanics, GATED sync** (state endpoints tested; no real-time sync implied) |
| **FM-060** | Portable case evidence bundle | P14 | FM-005 ✅, FM-022 ✅, FM-039 (partial) | **DONE** (bundle build/export with redactions; suite green) |

---

## 2. Recommended Pull Order (from feature map §4)

```
1. FM-048  Role and permission administration         ← START FIRST (no H1 deps)
2. FM-011  Sensitive case and field controls          ← START SECOND (unlocks FM-031, FM-035, FM-045)
3. FM-021  Configuration governance and publication history  ← START THIRD (unlocks FM-036, FM-040)
4. FM-031  Authorized structured case search          ← AFTER FM-011
5. FM-016  Queue administration and worker distribution    ← CAN RUN PARALLEL with #4
6. FM-017  Approval transitions and separation of duties   ← AFTER FM-016
7. FM-052  Failed-work and outbox operations           ← CAN RUN PARALLEL with #4/#5
8. FM-055  Versioned event catalog and reliable delivery  ← CAN RUN PARALLEL with #4/#5
9. Pilot-driven: FM-036 (Forms) OR FM-026/027 (Import) OR FM-039/040 (Records)
```

---

## 3. Parallelism Analysis

### At Slice Level (max 2 concurrent slices recommended)

| Can Run Together | Reason |
|------------------|--------|
| FM-016 + FM-052/055 | Different pillars (P02 vs P13), satisfied deps, minimal file overlap |
| FM-048 + FM-011 | Different pillars (P11 security vs P11 fields), sequential preferred but parallelizable |
| FM-021 + FM-016 | Different pillars (P10 vs P02), satisfied deps |

### Cannot Run Together (hard dependency)

> **2026-09-11: all rows below resolved.** FM-031/FM-017/FM-036/FM-026/FM-027/FM-039/FM-018 unblocked
> by H1; all H1 features now DONE.

| Blocked | Blocker | Why | Status |
|---------|---------|-----|--------|
| FM-031 | FM-011 | Search needs sensitive-field redaction | ✅ resolved → DONE |
| FM-017 | FM-016 | Approvals need queue assignment | ✅ resolved → DONE |
| FM-036 | FM-021 | Forms need config governance | ✅ resolved → DONE |
| FM-027 | FM-026 | Quarantine needs import pipeline | ✅ resolved → DONE |
| FM-040 | FM-039, FM-021 | Retention needs legal-hold + config governance | ✅ shipped → DONE |
| FM-018 | FM-014 | SLA escalation needs workflow engine | ✅ resolved → DONE |

### At Task Level (within a slice, `maxConcurrency=4`)

When creating an `orca` Run for a slice, decompose into 4–12 tasks with `[P]` markers. Examples:

**FM-016 (Queue Admin)** — ~8 tasks, 4 parallelizable:
```
T01 [P] Domain: Queue value object + assignment policies
T02 [P] Domain: QueueRepository port
T03     Infrastructure: QueueRepository impl + migrations
T04 [P] Application: Manage queues use cases (CRUD)
T05     Application: Worker distribution (round-robin, manual claim)
T06 [P] API: Queue admin endpoints + schemas
T07 [P] Frontend: Queue admin UI (list, create, assign workers)
T08     Integration: Wire into task creation (T014 dependency)
```

**FM-052/055 (Outbox + Events)** — ~10 tasks, 5 parallelizable:
```
T01 [P] Domain: OutboxEvent, DeadLetter, EventCatalogEntry
T02 [P] Domain: OutboxRepository, EventCatalogRepository ports
T03     Infrastructure: Repositories + migrations
T04 [P] Application: Outbox admin (replay, dead-letter, lag metrics)
T05     Application: Event catalog (register, version, subscribe)
T06 [P] API: /admin/outbox, /admin/events endpoints
T07 [P] Frontend: Outbox dashboard + event catalog browser
T08     Infrastructure: OutboxDispatcher hardening (retry/backoff)
T09     Integration: Health endpoint adapter status
T10     Tests: Failure injection + replay scenarios
```

---

## 4. Execution Commands

### Create a Run for FM-016 (Queue Admin)
```bash
orca orchestration run-create \
  --objective "FM-016: Queue administration and worker distribution" \
  --json
```

### Create a Run for FM-052/055 (Outbox + Events) — parallel with above
```bash
orca orchestration run-create \
  --objective "FM-052+055: Failed-work/outbox operations and versioned event catalog" \
  --json
```

### View/Run in Orca DAG viewer
```bash
npx orca-dag  # opens http://localhost:8787
```

### Validate local tests (no Docker needed)
```bash
cd backend
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q
```

---

## 5. Key Constraints to Remember

1. **Never block on Azure** — all external integrations are ports with local adapters; Azure placeholders exist in `infrastructure/*/azure_*_placeholder/`
2. **Docker is flaky** — always verify with `docker info` first; use sqlite profile for tests/CI
3. **Layer boundaries enforced** — `import-linter` will fail if domain imports infrastructure
4. **Tenant isolation mandatory** — every repository method takes `tenant_id`; tests assert cross-tenant failure
5. **Audit immutability** — four-layer enforcement (DB trigger, ORM listener, no app path, compensating events only)
6. **Synthetic data only** — no PHI, no real identifiers; `SYNTHETIC_ENVIRONMENT=1` marker in logs/UI

---

## 6. Quick Reference: What's Done vs Remaining (updated 2026-09-11)

| Category | Done (Slice 1 → H2) | Remaining (H1, not started) | Remaining (H3/Gated) |
|----------|---------------------|-----------------------------|----------------------|
| **Cases** | Intake, workspace, edit, CaseType pub, **ref formats, relationships** | — | Derived fields |
| **Workflow** | Lifecycle, transition, simulate, **queue admin** | **Approvals, SLA escalation** | Composition, visual designer |
| **Documents** | Upload, versioning, required guards, **protection/redaction/locking** | — | — |
| **Search** | **Structured, saved, in-case search** | — | Semantic |
| **Forms** | — | **Conditional case forms** | Visual designer, advanced execution |
| **Records** | Refusal seam, holds place, **retention/disposition/archival** | **Legal-hold lifecycle (full)** | Defensible deletion |
| **Audit** | Atomic + timeline, **export/corrections, access audit, signed batches** | — | SIEM streaming |
| **Security** | Auth + tenant isolation, **role admin, sensitive fields, break-glass** | Session controls (partially Gated) | AI boundary |
| **API/Events** | REST + OpenAPI + idempotency, **outbox ops, event catalog** | — | Outbound webhooks, GraphQL |
| **Import** | — | **Mapping + dry-run + quarantine** | Email/scan/webhook, connectors |
| **Reports** | **Operational, custom, dashboards** | — | — |

---

*Generated from `specs/pillars/01-deduplicated-feature-map.md` and `specs/001-.../tasks.md`*  
*Architecture: modular monolith with ports/adapters — Azure adapters are seams, not blockers*