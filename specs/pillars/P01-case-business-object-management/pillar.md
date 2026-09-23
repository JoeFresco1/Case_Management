# Pillar P01 — Case & Business Object Management

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> A Case is the primary unit of work. The platform models business objects generically and treats each as a first-class entity.

## 1. Pillar statement

This pillar owns the lifecycle, structure, and persistence of a Case and of any related **business objects** that are not cases but share first-class status (e.g., a `Claim` in an insurance deployment, an `AuthorizationRequest` in a healthcare deployment). The pillar's job is to make any business object:

- identifiable across time and tenants,
- typed by a published schema with a version,
- bound to a lifecycle with explicit transitions (delegated to P02),
- auditable end-to-end (delegated to P09),
- accessible only to authorized actors (delegated to P11).

The pillar does **not** own workflow execution, document storage, search, or reporting. It owns the *thing* that other pillars act on.

## 2. Scope

### In scope
- Case aggregate (identity, status, references, data, sensitivity, lifecycle).
- Business-object polymorphism: a Case Type is a published configuration that describes a category of business object with no hard-coded industry taxonomy.
- Per-tenant monotonic human-readable references.
- Optimistic concurrency control.
- Stale-write rejection.
- Case data validation against the published schema version pinned at creation.
- In-place field updates with audit history.
- Case relationships (parent/child, related, merged) — first slice supports simple relationships only.

### Out of scope (deferred to later slices)
- Cross-tenant case sharing.
- Mass reclassification of historical cases on a schema change.
- Hard delete (Constitution XIII — corrections are compensating events).

### Explicitly not building
- A "case object" that is also a workflow object, also a document, also a queue item, all in one entity. Cross-cutting concerns are explicit; the Case is its own aggregate and other pillars reference it.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `Case` | The primary unit of work. | Yes |
| `CaseType` | A category of case (e.g., "ServiceRequest", "PriorAuthorization"). | Yes |
| `CaseTypeVersion` | An immutable, published snapshot of a CaseType's schema. | Yes |
| `BusinessObject` (generic) | First-class non-Case objects (e.g., `Claim`, `Participant`, `Decision`). | Partial (Decision lands in P02, Participant in P03) |
| `CaseRelationship` | Directed edge between two cases. | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P01-US1**: As an intake user, I can create a case from a published Case Type with a stable reference, an initial workflow state, and an audit event, with no partial state if validation fails.
- **P01-US2**: As a case worker, I can open a case and see its current state, owner, due date, blocking reason, structured data, participants, documents, tasks, decisions, and timeline.
- **P01-US3**: As an authorized user, I can edit a case's permitted fields; stale writes are rejected with a clear conflict; field-level history is preserved.

### P2 — Important
- **P01-US4**: As a process administrator, I can mark a Case Type as sensitive and require additional authorization for views and edits.
- **P01-US5**: As a case worker, I can link a case to another case (parent/child, related) and unlink it; the relationship shows in both cases' workspaces.
- **P01-US6**: As a system identity, I can create a case via an approved API with an idempotency key; duplicate calls do not create duplicates.

### P3 — Deferred
- **P01-US7**: As an authorized user, I can split a case into two cases, preserving history and re-associating the audit trail.
- **P01-US8**: As an authorized user, I can merge two cases into one, with a clear record of which case superseded which.
- **P01-US9**: As a process administrator, I can configure per-tenant reference number formats (e.g., `C-{year}-{seq}` vs. `PR-REQ-{seq}`).

## 5. Cross-cuts

- **P05 (Metadata)**: a Case's `data` is a JSON document validated against the Case Type's published schema. Pillar P05 owns the schema language; P01 owns the runtime.
- **P09 (Audit)**: every state change writes an `AuditEvent`. P01 calls P09; it does not implement audit.
- **P11 (Security)**: every read and write is authorization-checked. P01 calls P11; it does not implement the policy.
- **P13 (API)**: Pillar P01 is exposed as REST resources under `/api/v1/cases`. P13 owns the contract; P01 owns the use cases.

## 6. Acceptance scenarios (representative)

- A1: An authorized intake user creates a case with valid required fields. The system creates the case with a stable reference, binds it to the published Case Type Version, sets the initial workflow state, and writes a `CaseCreated` audit event in the same transaction.
- A2: An authorized intake user submits a case missing a required field. The system rejects the submission with a structured `validation_failed` error identifying the field; no case, no workflow instance, no partial task is created.
- A3: Two clients attempt to update the same case field with the same `version_token`. The first succeeds; the second receives a `conflict` error and is told to refetch.
- A4: A user without view permission on a sensitive case attempts to read it. The system returns `forbidden` and writes an access-denied audit event.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements P01-US1, US2, US3 (partial) plus the foundations for P02, P03, P05, P09, P10, P11, P13. |
| `002-leftovers` Run C | Draft | Worker home (dashboard layout, preferences under an admin policy that wins) + separation plans that preserve the case page trail (validate-only split/join/merge, byte surgery deferred). See `specs/002-leftovers/architecture-runC.md`. |
| Future slice 2 | Planned | Case relationships (P01-US5), sensitive-case redaction (P01-US4). |
| Future slice 3 | Planned | Case split/merge (P01-US7, US8). |

## 8. Open questions

- Q1: When a Case Type's schema changes incompatibly, what is the user-visible behavior for cases bound to the old version? (Current intent: cases retain their old version; the new version applies only to new cases. No automatic migration. Migrations are an explicit, audited operation.)
- Q2: What is the cardinality budget for `case.data` (number of fields, total size)? Affects the database's JSON column strategy.
- Q3: Does the platform need cross-case uniqueness constraints (e.g., "no two open cases of type ServiceRequest with the same `external_ref`")? If yes, where do they live — P01 invariants or P10 rules?
