# Pillar P08 — Records & Retention

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Records-management discipline: legal hold, retention, archival, defensible disposition, all auditable.

## 1. Pillar statement

This pillar owns the **records-management discipline** that turns a working case-management platform into a defensible records system. The pillar governs:

- **Retention**: how long a record (case, document, audit event) is kept.
- **Legal hold**: a directive that prevents disposition regardless of retention.
- **Archival**: the move of a record to a different storage tier (still online, but cooler).
- **Disposition**: the explicit, audited destruction of a record at end of retention (subject to legal hold and to business rules).
- **Defensibility**: every disposition is auditable end-to-end, with a chain of custody and a documented policy that was active at the time of disposition.

The pillar is opinionated: records are not deleted casually. Disposition is an explicit operation, not a side effect of a configuration change.

## 2. Scope

### In scope
- Per-tenant retention policy: per case type, per document classification, per audit category.
- Legal hold: a case, document, or query result can be placed under hold; disposition is blocked.
- Hold release: explicit, audited.
- Archival: a record can be moved to a cooler storage tier; reads remain possible but the latency is higher and the cost is lower.
- Disposition: a record can be marked for disposition; the disposition is reviewed and either approved and executed or rejected.
- Disposition audit: every disposition is a `RecordDisposed` audit event with reason, policy version, approver.
- Retention policy versioning: a published retention policy is immutable; a new policy creates a new version.
- Hold-overrides-retention; only an explicit release of hold allows disposition to proceed.

### Out of scope (deferred)
- A full NARA / DoD 5015.2 records-management certification. (P3; we design for it but do not pursue certification in v1.)
- A standalone records portal. (P3; the records-management UI lives in the admin surfaces.)
- A "vault" service for highly sensitive records. (P3; P11 owns the seam.)

### Explicitly not building
- Silent deletion. Disposition is always explicit and audited.
- Disposition that bypasses legal hold. Hold is enforced at the storage layer.
- An "auto-delete after N days" feature without a disposition workflow.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `RetentionPolicy` | A versioned policy describing retention per category. | Deferred (P3) |
| `LegalHold` | A directive preventing disposition. | Deferred (P3) |
| `ArchivalTier` | A storage tier (hot, warm, cold) and its read latency / cost. | Deferred (P3) |
| `DispositionRequest` | A request to dispose of a record. | Deferred (P3) |
| `DispositionApproval` | An approval (or rejection) of a DispositionRequest. | Deferred (P3) |
| `RecordDisposed` | An audit event recording an executed disposition. | Deferred (P3) |
| `case.legal_hold` (flag) | A per-case flag that signals "do not dispose". | Yes (boolean in case record) |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P08-US1**: As a security administrator, I can place a case under legal hold; the system refuses to dispose of any case, document, or audit event associated with that case while the hold is active.
- **P08-US2**: As a security administrator, I can release a legal hold; the release is audited; subsequent disposition is allowed.

### P2 — Important
- **P08-US3**: As a records administrator, I can define a retention policy per case type, document classification, and audit category; the policy is versioned and published; existing records are evaluated under the policy that was active when they were created.
- **P08-US4**: As a records administrator, I can request disposition for a case; the request is reviewed and either approved and executed, or rejected with a reason.
- **P08-US5**: As a records administrator, I can move a case to an archival tier; the move is audited; the case remains readable but at a different latency.

### P3 — Deferred
- **P08-US6**: As a records administrator, I can run a defensible-deletion report for a period: which records were disposed, under which policy version, by whom, with what chain of custody.
- **P08-US7**: As a records administrator, I can define a "disposition freeze" for a date range (e.g., during a litigation window); no dispositions execute in the window.
- **P08-US8**: As a system, I can automatically detect orphan records (cases without recent activity past their retention) and propose them for disposition review.

## 5. Cross-cuts

- **P01 (Case)**: legal hold and archival state are properties of a Case.
- **P03 (Document)**: per-document retention and disposition are tracked here; the `DocumentStore` port is used for archival movement.
- **P09 (Audit)**: every hold, release, archival move, and disposition is an audit event.
- **P11 (Security)**: only authorized roles can place or release holds, request or approve dispositions.
- **P14 (Integrations & Migration)**: export to a long-term records archive is a P14 integration.

## 6. Acceptance scenarios (representative)

- H1: A security administrator places a case under legal hold. An attempt to dispose of any document associated with the case is rejected with a clear `legal_hold_active` error.
- H2: A security administrator releases the hold. The release is recorded as a `LegalHoldReleased` audit event. Subsequent disposition proceeds.
- H3: A retention policy says: "LabResult documents, 7 years." A LabResult document is 7 years and 1 day old. The system surfaces it as eligible for disposition; the records administrator reviews and disposes; the disposition is recorded.
- H4: A case is under hold. A bulk import attempts to overwrite the case. The import's `Disposition` action is rejected; the case is not modified.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice ships the `case.legal_hold` flag and the refusal semantics. No retention policy engine yet. |
| Future slice 2 | Planned | Retention policies, disposition workflow, archival tiers. |
| Future slice 3 | Planned | Disposition freeze, orphan detection, defensible-deletion reports. |

## 8. Open questions

- Q1: Is legal hold a binary flag, or can there be multiple holds per record (e.g., "litigation hold" + "regulatory hold")? (Current intent: P3 may extend; v1 is a single binary flag per case.)
- Q2: When a case under hold has its case type retired, does the hold remain? (Current intent: yes; holds are independent of case type lifecycle.)
- Q3: Is the archival tier a `DocumentStore` port selector, or a separate `ArchiveStore` port? (Current intent: P3 decision; v1 ships a single hot tier.)
