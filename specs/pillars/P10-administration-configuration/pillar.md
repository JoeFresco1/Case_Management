# Pillar P10 — Administration & Configuration

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Configuration is versioned product data, not code. Admins can design processes without engineering.

## 1. Pillar statement

This pillar owns the **configuration** of the platform: case types, case schemas, workflow definitions, rule sets, queues, SLA/calendar policies, notification templates, document classifications, access policies, and import mappings. Configuration is:

- **declarative** (no arbitrary code in the admin path; Constitution IX),
- **versioned** (every configuration artifact has versions; published versions are immutable),
- **governed** (lifecycle: draft → validating → published → retired),
- **inspectable** (a user or administrator can determine which configuration was active for any historical action),
- **testable** (a draft configuration can be simulated against synthetic cases before publication; the simulator returns a step trace with guard results and blockers).

The pillar is the boundary between "what the platform can do" (code) and "what this tenant does" (configuration).

## 2. Scope

### In scope
- Lifecycle for every configuration artifact: draft → validating → published → retired (and archived, where applicable).
- Validation: every publish is preceded by a validator that rejects ambiguous, unreachable, or non-terminating configurations.
- Versioning: published versions are immutable; changes create new versions.
- Configuration history: an admin can see which version was active, when, by whom, with what change summary.
- Configuration publishing is itself an audit event (P09).
- Test-before-publish: a draft configuration can be run through a deterministic simulator (P02) against synthetic cases; the result is shown before publication.
- Configuration distribution: a published version is made available to all instances of the platform within the tenant.

### Out of scope (deferred)
- A full authoring UI for configuration. (P3; v1 ships JSON files and a CLI.)
- Marketplace or importable configuration packs. (P3.)
- Per-tenant configuration override of platform defaults. (P3; v1 uses tenant-scoped configuration only.)

### Explicitly not building
- A "configuration" that is actually code. Declarative only.
- A "configuration" that runs at arbitrary points in the request lifecycle. Configuration is evaluated in well-defined places (workflow transitions, validation, routing).
- A "configuration change" that does not produce a new version. Every change is a new version.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `ConfigurationArtifact` | A versioned configuration object (base type). | Yes |
| `ArtifactType` (enum) | The kind of artifact (CaseType, WorkflowDefinition, Queue, ...). | Yes |
| `LifecycleState` (enum) | draft, validating, validated, published, retired. | Yes |
| `ConfigurationVersion` | An immutable snapshot of a configuration artifact. | Yes |
| `ChangeSummary` | A human-readable description of a change. | Yes |
| `ConfigurationAuditEvent` | An audit event for a configuration lifecycle action. | Yes |
| `Simulator` | A deterministic execution of a draft workflow against a synthetic case. | Yes (basic) |
| `BulkSimulator` | A runner that runs N synthetic scenarios and reports counts. | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P10-US1**: As a process administrator, I can create a draft workflow definition, run validation, and publish it as a new immutable version. Cases already using a prior version retain it.
- **P10-US2**: As a process administrator, I can create a draft Case Type with a schema, validate it, and publish it as a new immutable version.

### P2 — Important
- **P10-US3**: As a process administrator, I can simulate a draft workflow against a synthetic case and see the step trace (transitions considered, guards evaluated, actions fired, blockers) before publishing.
- **P10-US4**: As a process administrator, I can view a Case Type's version history, including author, approval, effective time, change summary, and dependent case types.
- **P10-US5**: As a process administrator, I can retire a published configuration; new cases cannot be created with the retired version; existing cases continue to use it.
- **P10-US6**: As a process administrator, I can configure a queue with a name, an assignment policy (manual, round-robin), membership rules, and visibility scope.

### P3 — Deferred
- **P10-US7**: As a process administrator, I can use a drag-and-drop workflow designer; the designer emits the same workflow definition format.
- **P10-US8**: As a process administrator, I can require approval by a second administrator before a configuration is published.
- **P10-US9**: As a process administrator, I can run a bulk simulator (e.g., 150 scenarios) and see pass/fail counts and the state that traps each failure.

## 5. Cross-cuts

- **P02 (Workflow)**: workflow definitions are configuration artifacts owned by this pillar; the engine is in P02.
- **P05 (Metadata)**: schemas are configuration artifacts owned by this pillar; the metadata semantics are in P05.
- **P07 (Forms)**: form definitions are configuration artifacts owned by this pillar.
- **P09 (Audit)**: every publish / retire / change is an audit event.
- **P11 (Security)**: only authorized roles can draft, validate, or publish.

## 6. Acceptance scenarios (representative)

- J1: A draft workflow with an unreachable state is rejected at validation time; the validator names the unreachable state.
- J2: A draft workflow with a human-driven cycle (`in_review → needs_info → in_review`) is accepted; a draft workflow with an automatic transition that loops without a breakable guard is rejected.
- J3: A published Case Type Version is published. An attempt to modify it is rejected. A new version is required.
- J4: A draft workflow is simulated against 10 synthetic cases. The report shows 8 pass, 2 fail; the 2 failures are listed with the unmet guard.
- J5: A Case Type is published. An admin retires it. New cases cannot be created with the retired version; existing cases continue to use it; the version is marked retired in the history.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships draft/publish lifecycle for Case Types and Workflow Definitions. Configuration is JSON-in-repo; no authoring UI. The simulator is in P02. |
| `002-leftovers` Run A | Draft | Governance history for role changes (FM-021 partial: authorship + audit trail per change). Impact/where-used, concurrent-edit conflict, and second-admin approval remain open — see `specs/002-leftovers/architecture-runA.md §6`. |
| Future slice 2 | Planned | Authoring UI (basic), bulk simulator, approval workflow. |
| Future slice 3 | Planned | Marketplace, per-tenant defaults, configuration-as-code export. |

## 8. Open questions

- Q1: When a configuration is republished, what is the relationship between versions? (Current intent: linear history; branching is P3.)
- Q2: Can a configuration be partially published (e.g., publish a workflow but not its companion rule set)? (Current intent: no — a workflow references its rule set; publishing the workflow implies publishing the rule set version it uses.)
- Q3: How is "draft" storage handled? In the same database as published, or in a separate draft store? (Current intent: same database with a `status` column; the draft is a regular row until published.)
