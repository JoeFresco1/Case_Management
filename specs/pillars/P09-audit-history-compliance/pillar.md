# Pillar P09 — Audit, History & Compliance

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Every meaningful action is explainable, durable, and tamper-evident.

## 1. Pillar statement

This pillar owns **audit** and **history**: the durable, queryable, tamper-evident record of meaningful activity in the platform. Audit answers: *what happened, when, who or what caused it, why, with which configuration, and where in the system?*

Audit is **append-only under all roles**, including the migration role. Corrections are not edits — they are compensating events. This is non-negotiable per the constitution. The pillar enforces it at four layers:

1. **Database**: a trigger rejects UPDATE and DELETE on `audit_events` for every role.
2. **ORM**: SQLAlchemy event listener that raises on any UPDATE/DELETE of an `AuditEvent`.
3. **Application**: there is no code path that attempts to modify or delete an `AuditEvent`.
4. **Migrations**: forward-only schema changes; historical corrections are compensating events written through the application layer.

Audit and operational telemetry (logs, metrics, traces) are **related but not interchangeable**. Operational telemetry may expire; audit does not.

## 2. Scope

### In scope
- Append-only `AuditEvent` with event type, timestamp, actor, identity type, organization/tenant context, subject, case reference, correlation/causation ids, configuration/workflow/rule version, source channel, reason, result, tamper-evidence metadata.
- Hash chain: each `AuditEvent.hash` is `SHA-256(prev_hash + canonical(payload))` for tamper evidence.
- Audit immutability at DB, ORM, application, and migration layers.
- Compensating events for corrections (`AuditCorrectionRecorded`).
- Per-actor and per-tenant audit query with pagination and filtering.
- Redaction: audit payloads reference protected values by id, never by content.
- The `AuditStore` port so audit can be backed by a different store in production (Azure Log Analytics, immutable blob, signed batches).
- A verification command (CLI) that reads the audit chain and asserts integrity.

### Out of scope (deferred)
- Real-time streaming of audit events to external SIEMs. (P14.)
- A "forensic search" UI. (P3.)
- Audit retention policy. (P8 territory; P9 owns the immutability, P8 owns the lifespan.)

### Explicitly not building
- An "audit log" that is actually a `print` statement. Audit is structured, persisted, queryable.
- An admin path that can edit or delete audit events. There is no such path.
- An audit that conflates operational telemetry and business audit. They are separate concerns.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `AuditEvent` | An immutable record of a significant action. | Yes |
| `AuditEventType` | The catalog of event types (versioned). | Yes |
| `AuditChain` | A helper for hash-chain verification. | Yes |
| `AuditStore` (port) | The audit persistence boundary. | Yes (DB table) |
| `AuditCorrectionRecorded` | A compensating event referencing the original. | Yes |
| `AuditQuery` | A controlled query shape (filters, sorts, limit, cursor). | Yes |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P09-US1**: As an authorized user, I can request a case's timeline; events are returned in chronological order with actor, timestamp, event type, and applicable configuration version.
- **P09-US2**: As a system, every state-changing use case writes exactly one `AuditEvent` in the same DB transaction as the domain change; if the audit write fails, the domain change is rolled back.
- **P09-US3**: As an operator, I can run a verification command that reads the audit chain and asserts that each event's hash matches the prior chain; a mismatch is reported and the operator can locate the affected event id.

### P2 — Important
- **P09-US4**: As an auditor, I can filter the timeline by event type, actor, date range, and case reference; results are paginated and never contain redacted payload content.
- **P09-US5**: As a process administrator, I can export the audit timeline for a case or a date range as a structured artifact; the export is itself an audit event.
- **P09-US6**: As a security administrator, I can see who accessed a sensitive case or document (successful and denied attempts).
- **P09-US7**: As an operator, I can place a corrective record on the timeline: the original event is preserved, and a new `AuditCorrectionRecorded` event references it with the reason and the corrected summary.

### P3 — Deferred
- **P09-US8**: As a system, I can stream audit events to an external SIEM in real time; the stream is best-effort and does not affect the persisted chain.
- **P09-US9**: As an auditor, I can request a signed batch of audit events; the batch carries a signature that the auditor's tooling can verify.
- **P09-US10**: As a process administrator, I can configure a custom event type for a tenant; the type is versioned and validated.

## 5. Cross-cuts

- **P01 (Case)**: the case timeline is the primary audit view.
- **P02 (Workflow)**: every transition is an audit event.
- **P03 (Document)**: every upload, version, and access is an audit event.
- **P10 (Administration)**: configuration changes are audit events.
- **P11 (Security)**: authorization decisions and access denials are audit events.
- **P13 (Events)**: domain events emitted to the message bus are a *different* stream from audit; they may overlap in content but have different retention and consumers.

## 6. Acceptance scenarios (representative)

- I1: A case is created. Exactly one `CaseCreated` event exists, with the actor, timestamp, case reference, case type version, and correlation id.
- I2: A case transition is requested. The transition and the `WorkflowTransitioned` event commit in the same transaction. If the audit write fails, the transition is rolled back; the system does not report a successful transition.
- I3: A user attempts to UPDATE an `AuditEvent` through the ORM. The attempt raises; the row is unchanged.
- I4: An operator runs `python -m app.cli verify-audit --tenant <id>`. The chain verifies; a hash mismatch is reported with the offending event id.
- I5: A user requests a case timeline. The response contains events in chronological order; sensitive payload values are not in the response.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships the audit port, the hash chain, the `AuditEvent` table with append-only trigger, and the `verify-audit` CLI. The timeline endpoint and per-actor queries arrive in T031+. |
| `002-leftovers` Run A | Draft | `ROLE_*` governance audit events (`RoleDefined`, `RoleAssigned`, …) extend the append-only chain, committed atomically with the domain change. Outbox fan-out for role events is prescribed but not yet wired — see `specs/002-leftovers/architecture-runA.md §3`. |
| `002-leftovers` Run B | Draft | `sod_outcome` on every `WorkflowTransitioned` row (FM-017), `SlaBreached`/`SlaWarningRaised` system-actor rows with `escalation_fired` (FM-018), `SavedSearchAudit` save+run rows (FM-032, `AUDIT_READ`-gated report). See `specs/002-leftovers/architecture-runB.md §3`. |
| `002-leftovers` Run C | Draft | Per-rendition `DocumentAccessed` rows (redacted vs original views distinguished), `RenditionBurned` / `DocumentRestricted` rows, bundle/link/checkout/re-index rows (`DocBundleCreated`, `ShareLinkMinted`, …), all committed atomically with the domain row + outbox. See `specs/002-leftovers/architecture-runC.md §3`. |
| Future slice 2 | Planned | Filtering UI, export, correction workflow. |
| Future slice 3 | Planned | Signed batches, SIEM streaming, custom event types. |

## 8. Open questions

- Q1: Is the audit chain global, per-tenant, or per-(tenant, subject)? (Current intent: per-tenant, with a "global" view that stitches tenant chains for cross-tenant auditors; v1 ships per-tenant only.)
- Q2: When a configuration version is retired, do audit events that reference it become unreadable? (Current intent: no — the configuration artifact is preserved for as long as audit events reference it.)
- Q3: Is "actor" always a user, or can it be a system identity? (Current intent: both; the `actor_type` field distinguishes.)
