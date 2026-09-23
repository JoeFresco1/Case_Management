# Pillar P12 — Reporting & Analytics

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Operational truth: reports that reflect current and historical state, distinguishable from each other.

## 1. Pillar statement

This pillar owns **reports and analytics**: the surfaces that turn the platform's data into operational, managerial, and compliance insight. The pillar prioritizes **operational truth** over a large dashboard catalog.

A report is either a **current snapshot** (a point-in-time view of current state) or a **historical aggregate** (a count or sum over a time window). The two are explicitly distinguished: a snapshot is not a sum, and a sum is not a snapshot. A report is **always sourced** from the system of record; it is not a separate datastore that can drift.

Reports do not modify state. They are read-only. Reports that surface sensitive data enforce the same authorization as the underlying read.

## 2. Scope

### In scope
- Operational reports for v1: open cases by type/status/queue/assignee/age; overdue tasks and breached SLAs; queue workload and aging; transition outcomes; import/reconciliation results; configuration publication history; audit/access activity; failed background work.
- Snapshot vs. aggregate distinction: every report declares its kind.
- Authorization: report results respect the actor's access (a case worker's report shows only their cases).
- Source timestamp: every report identifies when it was generated and from what data.
- Export: a report can be exported as a structured artifact (CSV, JSON); the export is itself an audit event.
- A `Report` port so reports can be backed by a different engine in production (Azure Synapse, Power BI, etc.).

### Out of scope (deferred)
- AI-generated insights or natural-language summaries. (P3; gated by Constitution XIV.)
- Self-service report builder. (P3.)
- Real-time streaming dashboards. (P3.)
- Embedded analytics. (P3.)

### Explicitly not building
- A report that contradicts the system of record. The system of record wins.
- A report that exposes data the actor is not authorized to see. Authorization is enforced.
- A report that is a separate copy of the data, with its own access controls and its own drift.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `ReportDefinition` | A versioned, declarative report spec (data source, filters, group-by, aggregations). | Deferred |
| `Report` (port) | The report engine abstraction. | Deferred |
| `SnapshotReport` | A point-in-time view of current state. | Deferred (basic counts in first slice) |
| `AggregateReport` | A count or sum over a window. | Deferred (basic counts in first slice) |
| `ReportExport` | A structured export of a report result. | Deferred |
| `ReportAuditEvent` | An audit event for a report generation or export. | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P12-US1**: As a supervisor, I can see the open-cases-by-status report for my team; the report identifies its source timestamp and is refreshed on demand.
- **P12-US2**: As a supervisor, I can see the overdue-tasks report for my team; overdue is computed from the system clock and the task's due date.

### P2 — Important
- **P12-US3**: As a process administrator, I can see the configuration-publication history (who published what, when, with which change summary) for my tenant.
- **P12-US4**: As a platform operator, I can see the failed-background-work report (which jobs failed, how many times, with what last error) and replay eligible failures.
- **P12-US5**: As a security administrator, I can see the audit/access activity report (who accessed what sensitive case/document, when, with what result).
- **P12-US6**: As a supervisor, I can export a report to CSV; the export is itself an audit event.

### P3 — Deferred
- **P12-US7**: As a process administrator, I can design a custom report using a controlled declarative spec (group-by, aggregations, filters, time window).
- **P12-US8**: As a manager, I can see a real-time dashboard (under 5s freshness) of operational KPIs.
- **P12-US9**: As a compliance officer, I can request a defensible-deletion report (P8 territory, surfaced through P12).

## 5. Cross-cuts

- **P01 (Case)**: open-cases reports.
- **P02 (Workflow)**: transition-outcome reports, SLA breach reports.
- **P08 (Records)**: defensible-deletion reports.
- **P09 (Audit)**: access-activity reports.
- **P10 (Administration)**: configuration-history reports.
- **P11 (Security)**: authorization is enforced on every report.
- **P14 (Integrations)**: report data may be exported to an external BI tool (Power BI, Synapse) via P14.

## 6. Acceptance scenarios (representative)

- L1: A supervisor requests the open-cases-by-status report. The response includes a `generated_at` timestamp, the counts per status, and the actor who requested it; the report is auditable.
- L2: A supervisor in queue A requests the overdue-tasks report. The result includes only tasks in queue A; no cross-queue data is leaked.
- L3: A platform operator requests the failed-background-work report. The result shows attempts, last error, and eligibility for replay; replay is a separate action and is itself audited.
- L4: A supervisor exports a report to CSV. The export is recorded as an audit event with the actor, report id, and timestamp.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships the `/health` endpoint as the only "report"; it is the foundation of operational visibility. No business reports yet. |
| Future slice 2 | Planned | P12-US1, US2, US3, US4, US5 — operational reports. |
| Future slice 3 | Planned | Custom report designer, real-time dashboards, BI export. |

## 8. Open questions

- Q1: Are reports cached? (Current intent: not in v1; each request reads the system of record. Caching is a P3 optimization with explicit invalidation.)
- Q2: Is there a "report" surface for AI-assisted insights? (Current intent: yes, in P3, gated by Constitution XIV — AI is bounded, attributable, and reviewable.)
- Q3: Is the source timestamp at the query or at the system of record? (Current intent: at the query; the report is "as of <query time>" with the system of record frozen for the duration of the query.)
