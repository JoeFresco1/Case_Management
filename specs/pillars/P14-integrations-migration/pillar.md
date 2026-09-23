# Pillar P14 — Integrations & Migration

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Connectors to enterprise systems; incremental migration from legacy platforms.

## 1. Pillar statement

This pillar owns the platform's **edges to the outside world**: connectors to enterprise systems, import/export pipelines, and the migration story that lets an organization move from a legacy platform to this one incrementally.

The pillar is governed by the constitution's strongest principles:

- **No unauthorized legacy access** (Constitution V.10, XII.5): no connector to OnBase, Panzura, or any enterprise system is enabled without explicit authorization, approved credentials, approved network access, and an approved integration design.
- **No OnBase clone** (Constitution XII): the new platform is not structurally dependent on the legacy one; integration is via documented contracts, not schema mirroring.
- **Incremental migration** (Constitution XI): the platform supports importing and reconciling selected case types while legacy systems remain operational.
- **Synthetic data in dev** (Constitution V.10, XIII.1): connectors are not exercised against real data in development; they are tested with synthetic fixtures.

A connector is a separately authorized, separately versioned, separately auditable component. A connector cannot be enabled merely because a URL or credential is present.

## 2. Scope

### In scope
- Connector framework: a connector is a separately deployable, separately authorized component that uses the P13 API surface.
- Connector lifecycle: design → approval → enable → operate → retire. Each step is an audit event.
- Import pipeline: dry-run validation, schema and mapping version checks, idempotency by source record id and batch, partial rejection, error reporting, counts and reconciliation, retry of safe failures, quarantine of malformed records.
- Export pipeline: structured export of cases, documents, and audit events under authorization; the export is itself an audit event.
- Migration mapping: a versioned document that maps source fields to target fields with optional transformations (declarative only).
- Reconciliation: source totals vs. destination outcomes (created, updated, skipped, rejected, failed).
- Coexistence: a case carries source-system references, ownership, migration state, and synchronization status without pretending unapproved real-time sync exists.

### Out of scope (deferred)
- Real-time bidirectional sync with a legacy system. (P3; v1 supports one-way, batched migration only.)
- A "connector marketplace". (P3.)
- An OnBase connector in the first slice. (Constitution V.10, XII.5; only after explicit authorization.)

### Explicitly not building
- A connector that uses undocumented proprietary semantics. The format is explicit, validated, and versioned; the connector infers nothing.
- A connector that bypasses the API surface. Connectors use the P13 API; they do not write to the database directly.
- A "smart migrator" that uses AI to interpret legacy data. The migration tool is deterministic; AI is a P3 optional reviewer.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `Connector` | A versioned, separately authorized integration component. | Deferred |
| `ConnectorDesign` | The design document for a connector (target system, scope, semantics). | Deferred |
| `ConnectorAuthorization` | The recorded authorization for a connector. | Deferred |
| `ImportJob` | A bounded ingestion from an approved source. | Deferred (P3) |
| `ImportBatch` | A batch within an ImportJob. | Deferred |
| `MappingVersion` | A versioned mapping from source to target. | Deferred |
| `ReconciliationReport` | Source totals vs. destination outcomes. | Deferred |
| `SourceSystemRef` | A reference carried on a migrated case to its source. | Deferred |
| `MigrationState` | A case's migration state (e.g., `imported`, `reconciled`, `live`). | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P14-US1**: As a system, the first slice ships with **no** enterprise connector enabled. The platform runs locally with synthetic data and no outbound calls to enterprise systems.

### P2 — Important
- **P14-US2**: As an operator, I can submit a validated import package (CSV, JSON, or JSONL) from an approved source, see dry-run results before finalizing, and reconcile outcomes against the source.
- **P14-US3**: As a process administrator, I can define a mapping document that says "source field `case.request_id` maps to target `case.data.requestId`"; the mapping is versioned and validated before use.
- **P14-US4**: As a system, an import package with a malformed record quarantines the record rather than failing the whole batch; the operator can inspect the quarantine and release or reject.

### P3 — Deferred
- **P14-US5**: As a security administrator, I can authorize a connector by recording an approval; the connector cannot be enabled without a current approval.
- **P14-US6**: As a process administrator, I can define a coexistence policy: a case carries source-system references, ownership, and migration state; the platform does not pretend unapproved real-time sync exists.
- **P14-US7**: As a security administrator, I can retire a connector; new imports are rejected, in-flight imports complete or fail safely; the retirement is audited.
- **P14-US8**: As an operator, I can export a case bundle (case, documents, audit timeline) to a portable archive for offline review or legal hold.

## 5. Cross-cuts

- **P01 (Case)**: migrated cases carry `SourceSystemRef` and `MigrationState` metadata.
- **P03 (Document)**: imported documents go through the `DocumentStore` port; content hashes are validated.
- **P04 (Capture)**: the import pipeline is a capture channel.
- **P09 (Audit)**: every import / export / connector enable is an audit event.
- **P11 (Security)**: connector enable / disable is a security operation; the connector uses an authorized service identity.
- **P13 (API)**: connectors use the API; they do not bypass it.

## 6. Acceptance scenarios (representative)

- N1: The first slice starts without any enterprise connector enabled; no outbound calls to enterprise systems occur during local development.
- N2: An operator submits a synthetic import package in dry-run mode. The system reports expected creates, updates, rejects, duplicates, and missing references without writing any case data.
- N3: An operator submits a finalized import package. The system creates cases, attaches documents, records audit events, and produces a reconciliation report; failures are quarantined, not silently lost.
- N4: A connector attempts to write directly to the database. The system rejects the action; the connector is required to use the API.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships with no enterprise connector. The connector framework is referenced but not implemented. |
| Future slice 2 | Planned | Import pipeline, mapping document format, reconciliation report. |
| Future slice 3 | Planned | Connector lifecycle, coexistence model, export bundle, OnBase connector (subject to explicit authorization). |

## 8. Open questions

- Q1: What import package formats are supported? (Current intent: CSV, JSON Lines, and JSON; versioned per the import spec.)
- Q2: Can a mapping document include code-based transformations? (Current intent: no — declarative only. Code-based transformation is a P3 extension.)
- Q3: Is the OnBase connector part of the public roadmap? (Current intent: yes, gated by explicit authorization, with a separate feature slice. The pillar lists it as "subject to explicit authorization" so we don't pretend it will exist by default.)
- Q4: When a connector is enabled, is the source system considered a "system of record" for any case? (Current intent: no — once a case is in the new platform, the new platform is the system of record. The source ref is provenance, not authority.)
