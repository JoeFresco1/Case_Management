# Feature Specification: Leftover Parity — Deep Coverage for OnBase Replacement

**Feature Branch**: `002-leftovers`
**Created**: 2026-09-11
**Status**: Draft
**Input**: Gap analysis between `C:\Users\Fresc\case_management_feature_recon\output\onbase_features.json` (589 evidence-backed features, 21 pillars, 130 CORE / 356 IMPORTANT) and `specs/pillars/01-deduplicated-feature-map.md` (60 canonical features FM-001..FM-060, H0 first slice ~15 FMs).
**Constitution**: `.specify/memory/constitution.md` v1.0.0 — highest authority. In particular XII (No OnBase Clone), XI (Incremental Replacement), XIII (Vertical Slices), XIV (Deterministic Core), V (Security Architectural).

## 1. Why this spec exists

`001-case-management-platform/first-slice/spec.md:9` ships a vertical `create -> route -> work -> document -> decision -> close -> timeline` covering P01, P02 (partial), P03 (partial), P05, P09, P10 (partial), P11 (foundation), P13 (foundation).

That leaves the majority of the recon inventory in H1/H2/H3/Gated or explicitly deferred. This spec is the **umbrella** that turns those leftovers into sliceable, shippable tracks. It is NOT one big-bang build — per `AGENTS.md §19` it must be split into 4–12 task DAGs per Run before implementation.

Terminology rule (Constitution XII, I): this spec uses **business terminology**. Hyland/OnBase terms appear only in `Migration source:` lines to preserve traceability, never as domain names.

## 2. Pillar scope

This slice draws requirements from all 14 pillars, with primary ownership per track:

| Track | Primary pillar(s) | Supporting | FM horizons covered | Recon pillars covered |
|---|---|---|---|---|
| T1 Capture & ingestion parity | P04, P14 | P01, P03, P05, P09, P11, P13 | FM-026, FM-027, FM-028, FM-029, FM-030 | Capture and Ingestion (83) |
| T2 Search, retrieval & metadata depth | P06, P05 | P01, P03, P09, P11 | FM-031, FM-032, FM-033, FM-034, FM-035, FM-011, FM-012 | Search and Retrieval (54), Metadata and Indexing (41) |
| T3 Forms runtime + authoring | P07 | P01, P02, P05, P10 | FM-036, FM-037, FM-038 | Forms (46) |
| T4 Workflow / case-model depth | P02, P01, P10 | P05, P09, P11, P13 | FM-013(ext), FM-014(ext), FM-016, FM-017, FM-018, FM-019, FM-020, FM-021, FM-009, FM-010 | Workflow (82), WorkView (65), Case Management (8) |
| T5 Document, viewer & worker UX | P03 | P01, P09, P11 | FM-023(ext), FM-024(ext), FM-025 | Document Management (23), Document Viewer (9), Document Tracking and History (14), Collaboration (2), Client UI (9) |
| T6 Records, audit evidence & reporting | P08, P09, P12 | P01, P03, P11, P13 | FM-039, FM-040, FM-041, FM-042, FM-044, FM-045, FM-046, FM-051, FM-053, FM-054 | Records Management and Retention (32), Audit and History (5), Reporting and Analytics (17) |
| T7 Admin, config & API surface | P10, P13 | P05, P09, P11 | FM-021, FM-047, FM-052, FM-055, FM-056, FM-057 | Administration (23), API and Extensibility (15) |
| T8 Connectors, channels & endpoints | P14, P04 | P09, P11, P13 | FM-052, FM-055, FM-056, FM-058, FM-059, FM-060 | Integrations (22), Mobile (13), Notifications (1) |

## 3. Track details

### T1 — Capture & ingestion parity

**Problem**: H0 ships only single-file upload (`FM-022`). Recon shows 23 CORE capture features with no equivalent.

**In scope (business capabilities, not legacy screens)**:
- Bulk import packages (CSV/JSON/JSONL) with versioned field mappings, dry-run preview (creates/updates/rejects/duplicates), commit, reconciliation report. Migration source: `DIP Process Job, Import Index File (Tagged/Ordered), Awaiting Commit -> Commit/Purge`.
- Watched-folder channel with subfolder-to-type mapping, filename-to-field parsing, scheduled pickup, poison-file isolation. Migration source: `Directory Import Processor, Subfolder Mapping, ERROR_FILES Folder`.
- Report-stream ingestion (line-data print streams split into documents via continuation rules). Migration source: `COLD/ERM Processor, Implied Continuation`.
- Mailbox channel (IMAP/Graph/Gmail via adapter) with account rules mapping message attributes to type/queue, unidentified-item holding area. Migration source: `Mailbox Importer, Account Rules, SYS Unidentified Emails`.
- Scan intake with device/source provenance, scan-batch inbox, page split/join/merge workspace, hold-until-verified. Migration source: `Document Imaging Scanning/Sweeping/Scan from Disk, Scan Queue, Batch Processing Layout, Batch Indexing`.
- Assisted indexing: barcode-driven type/field assignment with separator-page splitting; OCR text extraction (full-page + zonal + point-and-shoot); template-zone extraction (form ID zone + data zones); handwriting recognition as optional adapter; pre-population sets (primary -> secondaries). Migration source: `Bar Code Process, Full-Page OCR, Advanced Capture Form Zones, ICR, AutoFill Keyword Sets`.
- Capture-to-case routing: imported item auto-creates/routes case with provenance (source system, source id, channel, timestamp, payload hash). Verification-report-to-review-queue on exception.
- Per-record quarantine: malformed items isolated, inspectable, releasable/rejectable, never fail whole batch silently.

**User stories**:
- T1-US1 (P1): As an operator, I can submit a versioned import package in dry-run mode and see expected creates/updates/rejects/duplicates without writing case data. (FM-026)
- T1-US2 (P1): As an operator, I can commit an approved package and receive a reconciliation report; malformed records quarantine per-record. (FM-026, FM-027)
- T1-US3 (P2): As an administrator, I can enable/disable tenant channels and validation policies per channel. (FM-027)
- T1-US4 (P2): As an operator, I can triage a quarantine record (view reason/payload-hash, release or reject) with audit. (FM-027)
- T1-US5 (P3/Gated): As an operator, I can configure mailbox/scan/webhook channels; messages become documents/notes with provenance. (FM-028, FM-029, FM-030)

**Acceptance**:
- Dry-run writes zero case rows; commit writes cases + documents + audit + outbox atomically per record.
- Re-submitting same source-record id is idempotent (no duplicate case).
- ERROR/unidentified holding area is queryable; release/reject is audited with actor/reason.
- Scan/batch items carry device/source provenance; separation actions preserve page audit trail.

### T2 — Search, retrieval & metadata depth

**Problem**: H0 ships only filtered `GET /cases`. Recon shows keyword search, custom queries, full-text catalogs, saved searches, field-behavior flags as CORE/IMPORTANT.

**In scope**:
- Structured case search over reference, type, status, queue, assignee, priority, due date, sensitivity, participant ref + configured searchable fields; authorization-filtered at query time (never index-as-bypass); redacted snippets for sensitive fields. (FM-031, FM-011)
- Saved/reusable sorted searches per actor; search-activity audit (actor, query shape, timestamp, result count) inspectable by security role. (FM-032)
- In-case document search by metadata + full text without granting visibility beyond direct-access rights. (FM-033)
- Metadata authoring depth: per-type field flags (unique, required-on-create, required-on-retrieve, hidden, read-only), default values, dual-value/case-sensitivity options, pre-population sets with instances, note-type configuration. `Save As` semantics for type reuse. (FM-008 ext, FM-012)
- Semantic (AI-ranked) search as bounded assistance only: confidence + provenance shown, deterministic authorization authoritative, never auto-advances work. (FM-034, Constitution XIV)
- Break-glass cross-tenant auditor search: time-bounded, reason-bearing, prominently flagged on every resulting action. (FM-035)

**User stories**:
- T2-US1 (P1): As a worker, I can filter cases by core + searchable custom fields; invisible cases never appear; sensitive snippets redact. (FM-031)
- T2-US2 (P2): As a worker, I can save/reuse a sorted search. (FM-032)
- T2-US3 (P2): As a security admin, I can inspect who searched what with result counts. (FM-032)
- T2-US4 (P3): As a worker, I can full-text search documents within a case I can already open. (FM-033)

**Acceptance**:
- Unauthorized case never appears in results or snippets (two-tenant test per query path).
- Marking a field non-searchable removes it from future snippets; rebuild/ignore documented.
- Every search writes an audit event with query shape + count (no payload PHI).

### T3 — Forms runtime + authoring

**Problem**: Forms entirely deferred. Recon shows 5 CORE form features (designer, validation, field types, submission-to-lifecycle).

**In scope**:
- Versioned case-bound form definitions: sections/panels/repeating sections, field types (text/enum/date/attachment/calculated/lookup/signature/system-property), required/range/type validation, read-only/system fields, conditional visibility, tab order, custom validation messages, publish-time validation. Immutable once published; cases pin version. (FM-036)
- Execution: intake submission creates case; in-case step forms bind to workflow state with progress + transition validation; resumable multi-step; hidden-field data preserved safely; attachment control routes to document pipeline; lookup pre-population from configured sources. (FM-038)
- Authoring UI (H3): visual designer emitting the same validated definition format as the API; no second format. (FM-037)
- Signature seam: handwritten/typed signature field + external e-signature connector placeholder (no vendor SDK in domain). DocuSign/Adobe treated as P14 connectors.

**User stories**:
- T3-US1 (P1): As an intake user, I can submit a published case-bound form; validation failures name the field without losing entered data. (FM-036)
- T3-US2 (P2): As a worker, I can resume a multi-step form and submit a state-specific form that validates against the transition. (FM-038)
- T3-US3 (P3): As an admin, I can visually author the same validated form format. (FM-037)

**Acceptance**:
- Published form version immutable; draft validation rejects ambiguous conditions.
- Signature + attachment + calculated fields covered by deterministic tests; no arbitrary code in definition.

### T4 — Workflow / case-model depth

**Problem**: H0 ships sequential engine + basic SLA seam. Missing operational workflow + rich case modeling.

**In scope**:
- Queue operations: visible queues, claim/release/bulk-handle, assignment policies (manual + round-robin v1, extensible), membership/visibility scope, due-date/aging context, temporary delegation (e.g. PTO window). (FM-016)
- Approvals + separation of duties: consequential transitions require eligible second actor; originator self-approval blocked with explainable audit. (FM-017)
- SLA/escalation full: deadline state visible, breach deterministically fires configured actions (notify/supervisor-task/queue-move/exception-state) in timeline. Timers beyond SLA as explicit later feature. (FM-018)
- Advanced composition (H3): reusable subprocess fragments, parallel branches with explicit join semantics + validation. (FM-019)
- Visual designer (H3): same validated definition format, no second format. (FM-020)
- Config governance: authorship/approvals/effective-time/dependencies/retirement/history, optional second-admin approval, bulk simulator (N scenarios with pass/fail + trap-state report). (FM-021, FM-015 ext)
- Case-model depth: configurable references (validated format without changing internal id), relationships (relate/split/merge with provenance + bidirectional navigation), portfolio/grouping views, association/relationship attributes, filter prompts + dashboards backed by case model. (FM-009, FM-010)

**User stories**:
- T4-US1 (P1): As a worker, I can claim/release/delegate queue work with aging context. (FM-016)
- T4-US2 (P1): As an admin, I can require second-actor approval; self-approval is blocked + audited. (FM-017)
- T4-US3 (P1): As a worker, I see deadline state and breach escalation in the timeline. (FM-018)
- T4-US4 (P2): As an admin, I can inspect version history/dependencies/retirement and require second approval. (FM-021)
- T4-US5 (P3): As an admin, I can compose subprocesses + parallel joins with validation. (FM-019)

**Acceptance**:
- Human-driven cycles allowed; automatic runaway loops rejected at publish with named blocker.
- Delegation is time-bounded and audited; expiry restores prior assignment.
- Split/merge preserves both histories + provenance links; no silent loss.

### T5 — Document, viewer & worker UX

**Problem**: H0 ships upload/version. Missing viewer depth, lifecycle controls, and Unity-class worker ergonomics (rebuilt as modern web UX, not cloned ribbons).

**In scope**:
- Document protection: access restriction, versioned redaction (original preserved), filing lock (no modify/delete once filed), all audited. (FM-025)
- Viewer pipeline (browser-native): paged/thumbnail/detail modes, annotation layer (highlight/text-box/stamp/freehand), redaction burn as new rendition, revision/rendition history view, persistent checkout/checkout-history, concurrent-edit (keyword) locking, re-index action. Audio/video playback via adapter.
- Document operations: cross-document references (configurable display/sort), separation workspace (split/join/merge preserving audit), document package generation (multi-doc export with manifest), duplicate-by-hash detection, CSV export of result lists where authorized.
- Worker UX: configurable home/dashboard, favorites, personal layout shortcuts, list grouping/sorting, task pane equivalent (dockable case context), recent/history, import preview, user preferences + admin-enforced preference policy.
- Collaboration: permissioned bundles (envelope equivalent — user-created doc sets with independent sharing, not searchable by content), share-by-link/file with expiry + auth check.

**User stories**:
- T5-US1 (P2): As a worker, I can restrict/redact/lock a filed document without destroying history. (FM-025)
- T5-US2 (P2): As a worker, I can annotate/view revisions/checkout with history. (FM-023 ext)
- T5-US3 (P3): As a worker, I can bundle/share documents with independent permissions and expiry.

**Acceptance**:
- Redaction creates a new version; original retrievable under stricter right; both audited.
- Guessed-URL download of restricted doc returns `forbidden` with no metadata leak.
- Checkout/lock conflicts return structured `conflict` with holder + expiry.

### T6 — Records, audit evidence & reporting

**Problem**: Records/reporting almost entirely deferred; audit foundation exists but evidence/reporting surface missing.

**In scope**:
- Legal hold: place/release audited hold preventing disposition of case + docs + audit evidence. Refusal seam in H0 already planned; full lifecycle here. (FM-039)
- Retention: versioned policies by case type/doc class/audit category; eligible cases route through approval/rejection disposition workflow; archival tiers; freeze windows; orphan-record proposals without silent deletion. (FM-040, FM-041)
- Evidence: chain-of-custody report (what disposed, under which policy version, by whom/when); structured audit export + corrective annotations as append-only compensating events; access-decision audit (allow/deny with action/resource/actor/reason). (FM-042, FM-044, FM-045)
- External verifiability (Gated): SIEM streaming without weakening chain; signed batches on request. (FM-046)
- Operational reporting: open-case/overdue-task/failed-work reports scoped to team + authorization; governed export + custom groupings/aggregations/windows; fresh dashboards (KPIs with source-time, <5s freshness target). Distinguish current-state reports vs historical reports. (FM-051, FM-053, FM-054)

**User stories**:
- T6-US1 (P1): As a records admin, I can place/release a hold that blocks disposition. (FM-039)
- T6-US2 (P2): As a records admin, I can publish a retention policy and route eligible cases through disposition approval. (FM-040)
- T6-US3 (P2): As a supervisor, I can view team open/overdue reports scoped to my authorization. (FM-051)
- T6-US4 (P3): As a compliance user, I can obtain a chain-of-custody report. (FM-042)

**Acceptance**:
- Held case disposition attempted -> `refused_hold` with policy version cited; no partial delete.
- Every export/disposition writes its own audit event; original events never mutated.
- Dashboard shows source-time + authorization scope on every KPI.

### T7 — Admin, config & API surface

**Problem**: H0 ships JSON-in-repo + basic lifecycle. Missing operability + developer surface.

**In scope**:
- Config governance full (see T4): impact view (where-used for field/type/queue), concurrent-edit overwrite protection, `Save As` reuse, environment-value sets (dev/test/prod variables + template import). (FM-021)
- Diagnostics + system utilities: per-module logging with level/rotation/forwarding (ELK/Splunk sinks), per-web-app log forwarding, cache/service utilities behind `System Configuration` right. Storage admin: platter/cache/volume-equivalent abstraction + at-rest encryption toggle as adapter config, never domain logic.
- API depth: versioned REST preserved; outbound event catalog with versioned event types + backward-compat; reliable delivery via configured bus adapter; authenticated outbound webhooks with retry/dead-letter; governed ad-hoc query surface (GraphQL-equivalent) that cannot bypass tenant/field/document policy; extension/event-type registration as versioned modules with tenant-defined audit types governed + audited. (FM-047, FM-055, FM-056, FM-057)
- Ops: failed-work/outbox inspector (retry count, last safe error, dead-letter, lag) + safe replay. (FM-052)
- Legacy-compat decisions (explicit): no COM/.NET/ActiveX parity; SOAP/XML only via isolated adapter if a pilot requires it, never in domain; low-code app-builder treated as H3 designer consuming the same definition formats (T3/T4), not a separate runtime.

**User stories**:
- T7-US1 (P1): As an admin, I can see impact/where-used before publishing and am blocked on concurrent overwrite. (FM-021)
- T7-US2 (P1): As an operator, I can inspect/replay failed outbox work safely. (FM-052)
- T7-US3 (P2): As a developer, I can subscribe to versioned domain events with backward-compat guarantees. (FM-055)
- T7-US4 (P3): As an authorized subscriber, I receive authenticated retried webhook deliveries. (FM-056)

**Acceptance**:
- Concurrent config edit -> structured `conflict`, no silent overwrite.
- Replay of already-delivered event is idempotent; poison message dead-letters with safe error only.
- Ad-hoc query attempting unauthorized field/doc returns `forbidden`, never partial data.

### T8 — Connectors, channels & endpoints

**Problem**: H0 ships with no connector by design (`P14/pillar.md:57`). Replacement requires at least import/migration + a small set of named connectors, each separately authorized.

**In scope**:
- Approved connector lifecycle: design -> approval -> enable -> operate -> retire/drain, each step audited; connector uses P13 API only, never direct DB. (FM-058)
- Coexistence + migration state: source refs, ownership, provenance, migration state on case; truthful one-way batched migration only; no fake real-time sync. Portable case evidence bundle (case + docs + history) for offline/legal-hold review. (FM-059, FM-060)
- Named channel backlog (each a separate slice + explicit authorization, Gated by default): Office/Outlook add-in equivalent (file-from-mail with type/field binding), print-to-platform driver seam, desktop-bridge payloads (link/file with expiry), e-signature connector, ERP/CRM/health connectors (SAP S/4HANA federation pattern, Guidewire, Workday, Epic/Chart-search boundary documented as uncertain), Teams surfacing.
- Endpoint backlog: responsive web parity first; offline-capable field capture (briefcase equivalent: queue + fill offline, sync with conflict handling) and mobile retrieval/workflow/WorkView-object access as H3; push/email/SMS/fax outbound via P14 channel adapters with intent-first logging (roadmap open question #3).

**User stories**:
- T8-US1 (P2): As an operator, I can run an approved import with mapping version + reconciliation. (FM-026 via P14)
- T8-US2 (P3/Gated): As a security admin, I can approve/enable/retire a named connector with drain semantics. (FM-058)
- T8-US3 (P3): As an admin, I can define coexistence policy with source refs + migration state. (FM-059)
- T8-US4 (P3): As an operator, I can export a portable evidence bundle under authorization. (FM-060)

**Acceptance**:
- No connector enabled without current approval record; unapproved enable rejected + audited.
- Retire drains: new imports rejected, in-flight completes/fails safely, retirement audited.
- Bundle export is itself audited with policy version + actor + timestamp.

## 4. Functional requirements

- **FR-L01**: Every leftover track ships as a vertical slice (domain + application + infrastructure adapter + interfaces + tests + docs), never as a horizontal layer alone.
- **FR-L02**: Every mutation remains idempotent (`Idempotency-Key`), tenant-scoped (`tenant_id` in every repository method), policy-evaluated (deny by default), and audit-atomic (domain change + audit hash + outbox in one transaction).
- **FR-L03**: Published configuration (type, workflow, form, mapping, retention, queue, report) is immutable; changes create new versions; history shows author/approval/effective-time/change-summary.
- **FR-L04**: Search, report, and bundle outputs enforce authorization at result time and redact sensitive values by reference-id, never by content leakage.
- **FR-L05**: Capture channels carry provenance (source system, source id, channel, timestamp, payload hash) and fail to quarantine, never to silent loss.
- **FR-L06**: Deterministic core works without LLM; AI ranking/suggestion (search, extraction, authoring) is bounded assistance with logged recommendation + confidence + source context; human authorization remains authoritative.
- **FR-L07**: No PHI/secrets/production identifiers in fixtures/logs/audit payloads; `SYNTHETIC_ENVIRONMENT=1` marker preserved in local/test.
- **FR-L08**: OpenAPI is the contract for every new endpoint (`/api/v1/...`, `Page[T]` default 50 max 200, `ErrorResponse{code,message,details,correlation_id}`, `If-Match` where versioned); frontend types generated via `openapi-typescript`, never hand-typed.

## 5. Key entities (new vs reused)

Reused: `Case`, `CaseTypeVersion`, `WorkflowVersion`, `WorkflowInstance`, `Task`, `Queue`, `Document`, `DocumentVersion`, `AuditEvent`, `ConfigurationPublication`, `Outbox`, `IdempotencyKey`.
New in this spec: `ImportPackage`, `MappingVersion`, `CaptureChannel`, `QuarantineRecord`, `Provenance`, `SavedSearch`, `SearchAuditEvent`, `FormDefinition/Version`, `FormSubmission`, `Hold`, `RetentionPolicy`, `DispositionCase`, `EvidenceBundle`, `Connector`, `ConnectorAuthorization`, `CoexistencePolicy`, `ReportDefinition`, `DashboardKPI`, `EventTypeVersion`, `WebhookSubscription`, `Annotation`, `Redaction`, `CheckoutLock`, `DocBundle (envelope equivalent)`, `EnvironmentValueSet`.

## 6. Success criteria

- **SC-L01**: Each P1 leftover story (T1-US1/US2, T2-US1, T3-US1, T4-US1/US2/US3, T6-US1, T7-US1/US2) independently testable with automated acceptance in CI on sqlite profile (no Docker dependency).
- **SC-L02**: Two-tenant isolation test for every new repository/query; sensitive-redaction test for every new read path.
- **SC-L03**: Audit immutability (DB trigger + ORM listener + no-mutation-path) extended to every new event type; `verify-audit` replays clean.
- **SC-L04**: Coverage >=80% on `app/domain` + `app/application` including new tracks.
- **SC-L05**: `ruff + mypy --strict + import-linter + pytest` green per track DAG node.
- **SC-L06**: No file >1000 lines; no new `Any` outside ports; portable SQLAlchemy generics only.
- **SC-L07**: Architecture doc with Mermaid per converged track; decisions recorded as ADRs.

## 7. Assumptions

- Single-DB multi-tenant (`tenant_id` everywhere) retained; database-per-tenant is future scaling work.
- Outbox remains source of truth for events; WebSockets/SSE deferred.
- Local adapters (FS store, in-proc bus, header IdP) remain for dev/test; Azure adapters remain contracts until corp env available.
- Bulk binary formats (AFP, MICR sorter feeds, TWAIN/Kofax/ISIS direct drive) represented via file-drop + adapter seams in local dev, not live hardware.
- Exact OnBase REST endpoint catalog, license-tier matrix, and vendor-validated IdP list remain unknowns (`onbase_unknowns.md`) and do not block modern API design.

## 8. Out of scope / explicitly not building

- OnBase screen/terminology clone; DDS/Disk-Group internals; ActiveX/COM desktop; per-workstation capture services.
- Arbitrary code in workflow/form/mapping definitions; BPMN engine; LLM-authoritative consequential decisions.
- Real-time bidirectional legacy sync; connector marketplace; direct-DB connector access.
- Native mobile v1 (web parity first; offline capture as H3); real-time chat/social; e-fax hardware.
- Production Azure deployment in this spec (adapters as contracts + health distinction only).

## 9. Open questions

1. Which two H1 tracks pilot first — Import (T1) or Search (T2)? Recommendation in `01-deduplicated-feature-map.md:160` says security/governance/search/queues first; capture pilot may override.
2. Which named connectors (if any) are approved for T8, and who authorizes? Default: none without explicit security approval (Gated).
3. Do we need SOAP/XML compat adapter for a specific pilot, or REST-only? Default: REST-only unless a pilot proves otherwise.
4. Offline capture conflict semantics: last-write-wins with audit vs explicit merge? Current intent: explicit conflict response, no silent overwrite.
5. Retention clock semantics: pause-on-hold vs reset-on-release? Current intent: pause and resume, disposition clock frozen during hold.
6. Full-text engine choice (Postgres FTS vs Azure Cognitive Search vs OpenSearch) — adapter decision deferred to T2 slice; domain port unchanged.

## 10. Traceability

- Recon: `../case_management_feature_recon/output/onbase_features.json` (589), `onbase_taxonomy.json` (21 pillars), `onbase_replacement_matrix.md`, `onbase_user_journeys.md` (18 journeys), `onbase_unknowns.md` (34).
- Product: `../pillars/00-product-roadmap.md`, `../pillars/01-deduplicated-feature-map.md` (FM-009..FM-060 H1/H2/H3/Gated), each `PXX/pillar.md` Planned slices.
- Slice: `../001-case-management-platform/first-slice/spec.md` (H0 boundary).
