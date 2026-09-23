# Feature Specification: Case Management Platform — First Substantial Slice

**Feature Branch**: `001-case-management-platform`
**Created**: 2026-08-29
**Status**: Draft
**Last revised**: 2026-08-31 (added pillar cross-references)
**Input**: User description: "Build a secure, case-centric, configurable, versioned, auditable, API-enabled enterprise workflow platform that can run locally with synthetic data today, target Azure in production tomorrow, and incrementally replace selected legacy case-management capabilities without cloning or depending on the legacy system."

## Pillar scope of this slice

This feature slice implements a **subset** of the platform's 14 product pillars. Pillar scope is the authoritative product view; this slice is a vertical cut through several pillars. The pillar cross-cuts in `specs/pillars/00-product-roadmap.md` define which pillars depend on which.

| Pillar | What this slice ships |
|---|---|
| **P01 Case & Business Object Management** | Case aggregate, CaseType + CaseTypeVersion, monotonic reference, optimistic concurrency, edit with conflict. |
| **P02 Workflow, Tasks & Queues** | WorkflowDefinition + WorkflowVersion (draft/publish lifecycle), transition engine, tasks, queues (manual + round-robin), SLA clocks, simulator. |
| **P03 Document & Content Management** | Document + DocumentVersion, content-addressable storage behind the `DocumentStore` port, upload with validation, quarantine seam. |
| **P05 Metadata & Indexing** | Field definitions, schema validation, sensitivity labels, document classifications (basic). |
| **P09 Audit, History & Compliance** | Append-only `AuditEvent` with hash chain, immutability at DB / ORM / application / migration layers, compensating events, `verify-audit` CLI, timeline endpoint. |
| **P10 Administration & Configuration** | Lifecycle (draft → validating → published → retired), validation, simulator, configuration history. |
| **P11 Security & Identity** | `IdentityProvider` port, `UserContext`, `Policy` registry, tenant isolation in every repository, fail-closed for dev identity outside `local`. |
| **P13 API, Events & Extensibility** | REST API, OpenAPI, error model, idempotency keys, optimistic concurrency tokens, pagination, outbox, event catalog. |

Pillars **P04 Capture & Ingestion**, **P06 Search & Retrieval**, **P07 Forms**, **P08 Records & Retention**, **P12 Reporting & Analytics**, **P14 Integrations & Migration** are documented in their respective pillar specs and ship in later feature slices.

## Product Summary

A modular monolith case-management platform for regulated operational work. The Case is the primary domain object. Workflows, rules, and configuration are explicit, versioned, and testable. Documents are first-class metadata + storage-agnostic content. Tasks, queues, and SLAs are first-class. Audit is durable and explainable. The system runs locally with synthetic data behind ports/adapters; approved Azure adapters can be plugged in later without rewriting business logic.

This first slice delivers a representative end-to-end vertical: **create a case → route it → perform work → attach a document → make a decision → close the case → inspect history**, locally, deterministically, with full audit.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Intake user creates a well-formed case (Priority: P1)

As an **intake user**, I want to create a new case from a published case type so that the case begins in the correct initial state with a recorded reference, a workflow instance, an initial task, and an audit event — without understanding how storage or workflow internals work.

**Why this priority**: The Case is the primary domain object (Constitution I). Without a working case creation path, no other capability can be exercised end-to-end. This is the smallest demonstrable end-to-end vertical.

**Independent Test**: With a seeded case type and an authorized intake identity, submit a valid case payload via the API. Verify a case record, a workflow instance in the initial state, an initial task assigned to the configured queue, and a `CaseCreated` audit event all exist; verify the API returns a stable case reference.

**Acceptance Scenarios**:
1. **Given** a published case type, an authorized intake identity, and required fields supplied, **When** the user submits the case, **Then** the system creates a case with a stable reference, starts the configured workflow, creates the configured initial task, and emits a `CaseCreated` audit event.
2. **Given** the same setup but a required field missing, **When** the user submits, **Then** the system rejects the submission with a structured validation error identifying the missing field and creates no case, no workflow, and no partial task.

---

### User Story 2 — Case worker views a case workspace (Priority: P1)

As a **case worker**, I want to open a case and see its current state, owner, due date, blocking reason (if any), structured data, participants, documents, tasks, decisions, and timeline so I know what is true and what to do next.

**Why this priority**: View is the universal entry point for every downstream action. Without it, P1 stories for tasks, decisions, and documents cannot be performed by a human.

**Independent Test**: With at least one existing case that has participants, a task, a document, and a decision, fetch the case via the API as an authorized user; verify the response contains the required sections and respects field-level and document-level access controls.

**Acceptance Scenarios**:
1. **Given** an authorized identity and an existing case, **When** the user requests the case summary, **Then** the response includes reference, type, status, workflow state, priority, SLA state, assignee/queue, key participants, open tasks, recent documents, recent decisions, and recent timeline events.
2. **Given** a user without permission to view a sensitive document on a case, **When** the user requests the case summary, **Then** the document appears as "restricted" without content or identifying metadata that would leak restricted data.

---

### User Story 3 — Case worker completes a task and triggers a workflow transition (Priority: P1)

As a **case worker**, I want to complete an assigned task and have the workflow transition the case to the next state, create any follow-on tasks, and record the result in the timeline.

**Why this priority**: This is the first place where workflow engine behavior becomes observable. If a task can complete but no transition fires (or vice versa), the system is broken. This story is the smallest test of the workflow engine's transactional guarantee (Constitution III, IV, X, XIX).

**Independent Test**: With a case in a state that has an allowed transition requiring task completion, complete the task and assert the case moves to the next state, a `WorkflowTransitioned` audit event is written, and any follow-on tasks created by the transition are present.

**Acceptance Scenarios**:
1. **Given** a case in a state with an allowed transition whose guard requires the current task to be completed, **When** an authorized user completes the task, **Then** the case transitions to the next state, the task is marked completed, follow-on tasks are created as configured, and a `WorkflowTransitioned` event is emitted.
2. **Given** a case in a state with a transition guarded by a required document that is missing, **When** the user attempts the transition, **Then** the transition is rejected with a safe explanation that names the missing requirement, and no state change or partial task completion is recorded.

---

### User Story 4 — Case worker attaches a document to a case (Priority: P2)

As a **case worker**, I want to upload a document to a case so that the document's metadata is attached to the case record and the content is stored behind the configured storage adapter with a content hash, classification, and audit event.

**Why this priority**: Documents are first-class (Constitution X) but a working end-to-end can be demonstrated without a full document pipeline. P2 because the system can show value without it; P1 because without it the audit story is incomplete.

**Independent Test**: Upload a document to an existing case via the API; verify metadata is persisted, content is stored through the storage adapter (not in the relational store), a content hash is recorded, and a `DocumentAdded` audit event is written.

**Acceptance Scenarios**:
1. **Given** an authorized identity and an existing case, **When** the user uploads a document within configured size and type limits, **Then** the document metadata is created, content is stored through the storage adapter, a SHA-256 content hash is recorded, and a `DocumentAdded` event is emitted.
2. **Given** an upload exceeding the configured size limit or with a disallowed file type, **When** the user submits, **Then** the upload is rejected, no metadata or content is written, and an audit event of type `DocumentRejected` is recorded.

---

### User Story 5 — Authorized user records a decision on a case (Priority: P2)

As an **authorized user**, I want to record a decision (e.g., approve, deny, request more information) on a case with a required outcome, optional rationale, and references to evidence so that the decision is durable, attributable, and visible in the case timeline.

**Why this priority**: Decisions are explicit, attributable acts (Constitution IV). They become more important with workflow depth, but a v1 demo can ship a single decision type.

**Independent Test**: With an existing case in a state that allows a decision, post a decision; verify the decision is persisted with actor, timestamp, outcome, evidence references, applicable configuration version, and a `DecisionRecorded` event.

**Acceptance Scenarios**:
1. **Given** an authorized identity and a case in a state that allows decisions, **When** the user records a decision with an outcome and evidence references, **Then** the decision is persisted with full provenance and a `DecisionRecorded` event is emitted.
2. **Given** a decision type configured as requiring separation of duties, **When** the actor attempting the decision is the same identity that created the case, **Then** the decision is rejected with a "separation of duties" explanation and no decision is recorded.

---

### User Story 6 — Authorized user inspects a complete case timeline (Priority: P2)

As an **authorized user or auditor**, I want to see a chronological, filterable history of meaningful events for a case so I can answer "what happened, when, who, and why" (Constitution IV).

**Why this priority**: Without an inspectable timeline, auditability is theoretical. P2 because the data model is built into every earlier story; this story is the read-side.

**Independent Test**: With a case that has been created, transitioned, had a document added, and had a decision recorded, request the timeline; verify all four event types appear in order, each with actor, timestamp, and applicable configuration version.

**Acceptance Scenarios**:
1. **Given** a case with multiple historical events, **When** an authorized user requests the timeline, **Then** events are returned in chronological order with actor, timestamp, event type, and configuration/workflow/rule version where applicable.
2. **Given** a user without permission to view a sensitive case, **When** the user requests the timeline, **Then** the response is a structured `Forbidden` error and no event metadata is leaked.

---

### User Story 7 — Process administrator publishes a new workflow version (Priority: P3)

As a **process administrator**, I want to create a draft workflow, validate it, view a change summary, and publish it as a new immutable version so that existing cases retain their original workflow version and new cases use the new one.

**Why this priority**: Versioning is a Constitution III commitment but only becomes visible once at least one v1 workflow exists. P3 in the first slice — implement the data model and the publish lifecycle, defer a full authoring UI.

**Independent Test**: Create a draft workflow version, run validation (it should pass), publish it, then attempt to mutate the published version and verify the mutation is rejected.

**Acceptance Scenarios**:
1. **Given** a draft workflow definition, **When** an administrator runs validation and it passes, **Then** the version can be published, transitions to a `Published` lifecycle state, and emits a `ConfigurationPublished` event.
2. **Given** a published workflow version, **When** any user attempts to modify it, **Then** the modification is rejected; existing cases using the version continue to use it; new cases can be configured to use the new version.
3. **Given** a draft workflow with an unreachable state or an ambiguous transition, **When** validation runs, **Then** validation fails and identifies the configuration defect.

---

### User Story 8 — Developer runs the platform locally with synthetic data (Priority: P1)

As a **developer**, I want to start the platform on my personal computer with one command, have it load a synthetic data seed, and exercise the first-slice workflows without contacting Azure, OnBase, or any enterprise system.

**Why this priority**: This is the prerequisite for every other story being testable. It is also the explicit Constitution VI and the explicit "out-of-scope safeguards" in section 29.

**Independent Test**: With a clean checkout, run the documented local-start command. Verify the application starts, the synthetic environment marker is displayed, no outbound network calls occur to corporate endpoints, and a synthetic case type plus case are available.

**Acceptance Scenarios**:
1. **Given** a clean local environment with no corporate network access, **When** the developer runs the documented local-start command, **Then** the application starts, loads synthetic seed data, displays a "SYNTHETIC ENVIRONMENT" marker, and supports case creation, workflow execution, task completion, and timeline viewing without contacting any external system.

---

### Edge Cases

- What happens if two API clients submit the same case creation with the same idempotency key? — Both must result in exactly one case; the second request returns the prior result.
- What happens if a workflow transition request is retried after the request succeeded but the response was lost? — The retry must be a no-op that returns the prior result, not a duplicate transition.
- What happens if a document upload exceeds the configured size limit mid-stream? — The upload is aborted, no partial content is stored, and the document metadata row is not created.
- What happens if a required user identity is missing from a request context? — The request is rejected with `Unauthorized`; no record is created (Constitution V, fail closed).
- What happens if the local storage adapter is unavailable during an upload? — The case creation/transition that depended on the upload is rolled back; the user sees a `Dependency unavailable` error; an operator-visible failed-work record is created.
- What happens if an audit event write fails? — The domain change that depended on it is rolled back; the system never reports a successful transition that lacks an audit record.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** (Create case): The system MUST allow an authorized intake user (or an approved API client with an idempotency key) to create a case for a published case type. The system MUST assign a stable case identifier and human-readable reference, record the case-type version, set the initial workflow state per configuration, and emit a `CaseCreated` audit event atomically.
- **FR-002** (View case summary): The system MUST return, for an authorized requester, a case summary including reference, type, status, workflow state, priority, SLA state, assignee/queue, key participants, open tasks, recent documents, recent decisions, and recent timeline events — and MUST NOT include content the requester is not authorized to see.
- **FR-003** (Edit case data): The system MUST enforce field-level and state-level permissions, validate against the active schema, record material changes in history, and reject stale writes via an optimistic concurrency token.
- **FR-004** (Manage participants): The system MUST allow add/update/remove of participants per case-type rules. Removing a participant MUST NOT erase historical references in audit, decisions, communications, or documents.
- **FR-005** (Upload document): The system MUST validate file type and size, persist metadata, store content through the `DocumentStore` port, record a SHA-256 content hash, and emit a `DocumentAdded` audit event.
- **FR-006** (Document versions): The system MUST allow adding a new document version without destroying prior versions and MUST identify the current version.
- **FR-008** (Start workflow): On case creation, the system MUST create a workflow instance from the published workflow version, enter the initial state, create configured initial tasks, and start configured timers.
- **FR-009** (Workflow transition): The system MUST verify the transition exists from the current state, verify actor permissions, evaluate guards deterministically, apply the transition atomically with its required state changes and audit event, and emit a `WorkflowTransitioned` event.
- **FR-010** (Explain rejected transition): When a transition is rejected, the system MUST return a structured, safe explanation identifying the unmet requirement (missing field, missing document, insufficient permission, unresolved task, unmet approval, failed rule) without exposing data the requester cannot see.
- **FR-011** (Tasks): The system MUST create tasks from workflow actions and MUST support idempotent task completion (a duplicate completion with the same idempotency key MUST NOT create a duplicate downstream transition).
- **FR-013** (Routing rules): The system MUST route cases/tasks using published routing rules and MUST record the rule identifier, version, inputs (minimal), result, and correlation identifier. If routing cannot determine a valid destination, the system MUST route to an explicit exception queue.
- **FR-015** (Version workflows): The system MUST support draft → published lifecycle. Published versions MUST be immutable.
- **FR-022** (Case timeline): The system MUST provide a chronological timeline of meaningful events with actor, timestamp, event type, and applicable configuration version.
- **FR-025** (Access policies): The system MUST support role-based, case-type, queue, case, field, document, and action-level restrictions and enforce them server-side.
- **FR-030** (APIs): The system MUST expose versioned APIs for cases, tasks, documents, decisions, audit queries, configuration, and health. Mutations MUST support idempotency keys. List endpoints MUST paginate with a maximum page size.
- **FR-031** (Events): The system MUST publish documented domain events with stable identifiers, versions, timestamps, correlation/causation identifiers, and minimal payloads (no unrestricted PHI).
- **FR-032** (Failed background work): The system MUST record retries, failures, and dead-letter status for asynchronous work; operators MUST be able to find and replay eligible failures safely.
- **FR-033** (Configuration history): The system MUST allow inspection of current, published, retired, and archived configuration versions with author, approval, effective time, and change summary.
- **FR-034** (Health): The system MUST expose an authenticated health endpoint distinguishing local development adapters from Azure production adapters.
- **FR-036** (Synthetic data): The system MUST ship a synthetic-data generator and seed that produces clearly marked non-real data.

### Key Entities

- **Case**: the primary unit of work. Has stable id, reference, case-type-id + version, current workflow state, status, priority, owner/queue, sensitivity label, structured data, timestamps.
- **CaseType** (+ **CaseTypeVersion**): the schema and workflow binding for a category of case. Published versions are immutable.
- **WorkflowDefinition** (+ **WorkflowVersion**): states, transitions, guards, actions. Published versions are immutable.
- **WorkflowInstance**: the execution of a specific workflow version for a case. Records current state, completed transitions, active timers, correlation id.
- **Task**: a unit of work within a case. Has case id, type, status, assignee/queue, priority, due date, required inputs, idempotency key on completion.
- **Document** (+ **DocumentVersion**): metadata + content-addressable storage. Content held behind the `DocumentStore` port.
- **Participant**: a person/org/team related to a case. Has role, display name, stable id (when known), relationship metadata.
- **Decision**: an attributable act with outcome, rationale, evidence references, applicable configuration version.
- **AuditEvent**: an immutable record of a significant action, with actor, timestamp, correlation/causation ids, applicable config version, tamper-evidence metadata.
- **ImportJob** (+ **MappingVersion**): a bounded ingestion from an approved source with dry-run support and reconciliation results.

### Cross-cutting Infrastructure Ports (Constitution VI)

- `CaseRepository`, `WorkflowInstanceRepository`, `TaskRepository`, `DocumentRepository`, `ParticipantRepository`, `DecisionRepository`, `AuditEventStore`, `ConfigurationRepository`, `ImportJobRepository`
- `DocumentStore` (filesystem local, Azure Blob production)
- `MessageBus` (in-process + durable outbox local, Azure Service Bus production)
- `IdentityProvider` (seeded local identities, Microsoft Entra ID production)
- `SecretProvider` (env-var/local dev store, Azure Key Vault production)
- `Clock` (injectable; UTC in production)
- `Telemetry` (structured local logs + OpenTelemetry-compatible, Azure Monitor/App Insights production)

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer can run the platform locally with synthetic data and exercise the first-slice vertical (create case → transition → attach document → record decision → close case → view timeline) in a single uninterrupted session, on a personal computer, without any outbound network call to corporate endpoints.
- **SC-002**: 100% of workflow transitions in the first slice are accompanied by a corresponding audit event in the same transaction (no transition is reported successful without its audit event committed).
- **SC-003**: A duplicate mutation request carrying the same idempotency key produces exactly one domain effect and a consistent result on the second call.
- **SC-004**: Stale writes (older concurrency token) are rejected with a structured `Conflict` response and never silently overwrite newer state.
- **SC-005**: A published workflow version cannot be modified through any user-facing path; attempts are rejected.
- **SC-006**: At least one acceptance scenario from each P1 and P2 user story is automated and passes in CI using only local adapters.
- **SC-007**: Synthetic seed data loads in under 10 seconds on a typical developer laptop; an ordinary case summary returns in under 500 ms with the seed dataset.
- **SC-008**: No PHI, secrets, or production identifiers appear in any log line, audit event payload, or default test fixture.

---

## Assumptions

- The first slice targets a single configurable case type (a representative "intake → review → decision → close" flow) and a single workflow version, with the data model supporting additional types and versions without schema rewrites.
- The implementation language and framework for the first slice will be decided in the planning step (see Open Questions). Local-first constraints make this an internal decision driven by the developer's environment.
- The local relational store is a SQL-compatible open-source database; the production target is Azure SQL. The schema will use only portable types.
- The local document store is a content-addressable filesystem layout; production is Azure Blob Storage behind the same `DocumentStore` port.
- The identity provider for local development is seeded development identities; the production target is Microsoft Entra ID behind the same `IdentityProvider` port.
- The first slice does not implement a full authoring UI for case types or workflows; configuration is supplied as structured JSON/YAML files committed to the repository and validated by the configuration lifecycle.
- The first slice does not include real-time notifications; communication is recorded as intent only. Production notification channels are an Azure adapter concern, not a domain concern.
- The first slice does not include a real connector to OnBase, Panzura, or any enterprise system. Migration is represented by synthetic import packages validated by the import pipeline.
- AI is not part of the first slice. The platform must be fully useful without any AI component.
- Browser-based UI is in scope for the first slice as a minimal case workspace; rich queue/config/search UIs are deferred beyond P2.

## Resolved Decisions (from `/speckit-clarify`)

1. **Backend**: Python 3.12 + FastAPI + SQLAlchemy 2.x (async) + Pydantic v2 + Alembic
2. **Frontend**: React 18 + Vite + TypeScript
3. **Local database**: PostgreSQL 16 in Docker (development only; schema uses SQLAlchemy generic types for Azure SQL portability — see plan § Data Portability)
4. **Workflow format**: Custom JSON schema
5. **Rule language**: Declarative JSON conditions
6. **API style**: REST + OpenAPI (types generated for the frontend)
7. **Audit integrity**: Append-only with chained SHA-256 (upgrade to signed batches later)
8. **Tenancy**: Multi-tenant from day one (explicit `tenant_id` in every repository method, not magic)

Remaining open questions (deferred to later phases):

- **Standalone rules CRUD lifecycle** (deferred) — for v1, rules are embedded inside workflow version JSON. A standalone `/admin/rules` lifecycle endpoint is a separate future spec.
- **Notification channels** (deferred to a later slice — start with an in-process log sink)
- **Full-text search engine** (deferred to a later slice — start with portable `LIKE`)
- **E-signature, e-fax, real document scanning** (deferred to integration phase)
- **Production Azure hosting selection** (deferred to deployment architecture review)

### Polish decisions (from `/speckit-analyze`)

- Audit events are append-only under all roles, including the migration role. Corrections are compensating events only.
- `mypy --strict` covers `app/domain` and `app/application`.
- `.python-version` (3.12) and `.nvmrc` (20) pin the toolchain.
- The SQLAlchemy tenant-injection listener is opt-in via a config flag and is never the sole tenant-isolation mechanism; explicit `tenant_id` in every repository method is primary.

## Out of Scope (first slice)

- Recreating OnBase features or screens.
- Connecting to OnBase, Panzura, or any enterprise system.
- Real PHI, real production data, or copied production credentials.
- Real-time outbound notifications to email/SMS/fax.
- A complete authoring UI for case types and workflows.
- AI-assisted features.
- A production Azure deployment.
- High availability, multi-region, or autoscaling.

---

## Phased Implementation Plan (carried forward from the input)

- **Phase 0 — Foundation**: domain vocabulary, module boundaries, local-only profile, synthetic data policy, ports/adapters, test conventions, baseline authorization and audit.
- **Phase 1 — Case and timeline core**: case types, schemas, creation, viewing, participants, updates with optimistic concurrency, timeline/audit, basic search, synthetic demo flow.
- **Phase 2 — Documents and tasks**: document metadata and local content storage, document versions and access checks, task model and task workspace, basic queues and assignment, required-document rules.
- **Phase 3 — Workflow and rules**: draft/publish workflow versions, deterministic transition engine, validation/routing rules, task and SLA actions, workflow simulation, exception paths and retries.
- **Phase 4 — Administration and operations**: configuration lifecycle and approvals, queue administration, effective-permission inspection, background work operations, event/outbox delivery, operational reports.
- **Phase 5 — Migration and interoperability**: versioned import package format, dry-run validation, mapping and reconciliation, synthetic coexistence demonstration, document and case provenance, approved integration contracts.
- **Phase 6 — Azure deployment readiness**: approved Azure adapters, Entra integration, Azure SQL + Blob, Service Bus + outbox, Key Vault, Azure Monitor/App Insights, production security/privacy/resilience review.
- **Phase 7 — Controlled production adoption**: pilot one bounded process, validate operations and support model, backup/recovery and retention controls, additional slices, AI only after deterministic workflows and governance are proven.
