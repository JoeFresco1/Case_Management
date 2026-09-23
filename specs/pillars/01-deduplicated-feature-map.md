# Deduplicated Feature Map

**Status**: Draft  
**Owner**: Platform team  
**Created**: 2026-08-31  
**Source**: The 126 priority-tiered user stories in Pillars P01-P14

This document converts the product pillars into canonical, shippable feature candidates. Pillars remain long-lived capability and ownership boundaries; a row in this map is the default unit for a Spec Kit lifecycle (`specify` through `implement` and `converge`).

The map is deliberately deduplicated. When several pillars describe the same user outcome from different angles, the outcome appears once under a primary owning pillar and the other pillars are supporting owners. The source-story column preserves traceability to every pillar story.

## 1. How to use the map

1. Select one feature whose prerequisites are stable and whose horizon matches the current roadmap.
2. Use the feature name, outcome, boundaries, source stories, and pillar cross-cuts as the input to `/speckit-specify`.
3. Give the resulting spec one primary pillar. Supporting pillars contribute requirements and acceptance criteria; they do not receive duplicate specs.
4. Link the new `specs/NNN-short-name/` directory from this map and from the primary pillar's **Planned slices** table.
5. Split a candidate only if clarification shows two independently deployable outcomes with different actors or release timing. Do not split merely by API, UI, database, or module layer.

Shared technical behavior is normally folded into the business feature that needs it. For example, the case-creation endpoint, idempotency behavior, schema validation, and audit event are one **Case intake** feature rather than separate P01, P04, P05, P09, and P13 features.

### Horizon labels

| Horizon | Meaning |
|---|---|
| **H0 — Active foundation** | Explicitly included, wholly or partly, in `001-case-management-platform`. Do not open a duplicate spec unless a remaining outcome is intentionally separated from 001. |
| **H1 — Next** | The next layer of security, operational completeness, or directly usable product value after the first slice. |
| **H2 — Expansion** | Important breadth or governance after the core workflows are stable. |
| **H3 — Advanced** | Deferred sophistication, scale, alternate interfaces, or advanced administration. |
| **Gated** | Requires explicit authorization, infrastructure access, or a product decision before specification. |

Horizon is sequencing guidance, not implementation status. The codebase and the active feature's convergence results determine actual completeness.

## 2. Canonical feature catalog

### Foundation, cases, and metadata

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-001** | Local-safe platform runtime | Developers can run the platform with synthetic identities and data without enterprise connections; production-like environments reject development identity mechanisms. Excludes real enterprise connectors. | P11 | P14 | P11-US3, P14-US1 | H0 |
| **FM-002** | Authorization and tenant-isolation foundation | Every protected path is policy-evaluated, deny-by-default, and tenant-scoped, including indirect persistence and listener paths. Excludes the administrator-facing role editor. | P11 | P01, P03, P09 | P11-US1, P11-US2 | H0 |
| **FM-003** | Atomic, tamper-evident audit foundation | State changes and their audit records commit atomically, and operators can verify the per-tenant hash chain. Excludes filtering, exports, and external distribution. | P09 | P01, P02, P11 | P09-US2, P09-US3 | H0 |
| **FM-004** | Consistent API and generated-client contract | API consumers receive safe structured errors and frontend consumers use generated contract types. Business-specific endpoints remain part of their owning features. | P13 | P11 | P13-US3, P13-US4, P13-US5 | H0 |
| **FM-005** | Case intake and idempotent creation | An authorized human or system creates exactly one complete case from a published type, with validation, reference, initial state, audit, and replay-safe API behavior. Excludes form authoring and bulk import. | P01 | P02, P04, P05, P09, P11, P13 | P01-US1, P01-US6, P04-US1, P05-US2, P13-US1, P13-US6 | H0 |
| **FM-006** | Case workspace and read contract | A worker opens one authorized case and sees its consolidated operational state through a consistent read API. Excludes cross-case search and report aggregation. | P01 | P02, P03, P09, P11, P13 | P01-US2, P13-US2 | H0 |
| **FM-007** | Safe case editing and conflict recovery | Authorized edits are schema-validated, field-history preserving, and protected against stale writes with a recoverable conflict response. | P01 | P05, P09, P11, P13 | P01-US3, P13-US7 | H0 |
| **FM-008** | Case-type schema publication | Administrators draft, validate, and publish immutable case-type schema versions while existing cases remain bound to their original version. History, retirement, and approval governance belong to FM-021; visual form design is excluded. | P05 | P01, P09, P10, P11 | P05-US1, P10-US2 | H0 |
| **FM-009** | Tenant-configurable case references | Administrators configure validated human-readable reference formats without changing immutable internal identity. | P01 | P05, P10 | P01-US9 | H2 |
| **FM-010** | Case relationships and restructuring | Authorized workers relate, split, and merge cases while preserving bidirectional navigation, provenance, and audit history. Each operation may be separated during specification if release timing differs. | P01 | P09, P11 | P01-US5, P01-US7, P01-US8 | H2 |
| **FM-011** | Sensitive case and field controls | Administrators classify cases and fields as sensitive; unauthorized responses, logs, events, and search snippets redact values, with an encryption-at-rest seam for designated fields. | P11 | P01, P05, P06, P09 | P01-US4, P05-US3, P05-US7, P11-US6 | H1 |
| **FM-012** | Derived fields and durable field identity | Administrators define constrained derived values and rename field labels without losing stable field identity or historical meaning. | P05 | P01, P09, P10 | P05-US6, P05-US8 | H3 |

### Workflow, tasks, and configuration

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-013** | Versioned workflow definition lifecycle | Administrators draft, validate, and publish immutable workflow versions; running cases remain pinned to the version that started them. | P10 | P02, P05, P09, P11 | P02-US2, P10-US1 | H0 |
| **FM-014** | Atomic task completion and workflow transition | A worker completes a task, guards are evaluated deterministically, the case advances, follow-on work is created, and audit/events commit atomically. | P02 | P01, P05, P09, P11, P13 | P02-US1 | H0 |
| **FM-015** | Workflow simulation and scenario validation | Administrators run single or bulk synthetic scenarios and receive deterministic traces, blocker locations, and pass/fail summaries before publication. | P02 | P05, P10 | P02-US5, P10-US3, P10-US9 | H0 |
| **FM-016** | Queue administration and worker distribution | Administrators configure visible queues and assignment policies; workers see, claim, release, bulk-handle, and temporarily delegate work with due-date and aging context. | P02 | P09, P10, P11 | P02-US3, P02-US9, P10-US6 | H1 |
| **FM-017** | Approval transitions and separation of duties | Consequential transitions can require an eligible second actor, and the system prevents originator self-approval with an explainable audit record. | P02 | P09, P10, P11 | P02-US4, P11-US5 | H1 |
| **FM-018** | SLA deadlines and escalation | Workers see deadline state, and a breach deterministically triggers configured escalation actions visible in the case timeline. General-purpose timers are excluded. | P02 | P09, P10, P13 | P02-US6 | H1 (basic seam in H0) |
| **FM-019** | Advanced workflow composition | Administrators compose reusable subprocesses and parallel branches with explicit join semantics and validation. | P02 | P05, P10 | P02-US7, P02-US8 | H3 |
| **FM-020** | Visual workflow designer | Administrators visually author the same validated workflow definition consumed by the engine; the UI does not introduce a second definition format. | P10 | P02, P05 | P10-US7 | H3 |
| **FM-021** | Configuration governance and publication history | Administrators inspect authorship, approvals, effective time, dependencies, retirement, and publication history, with optional second-admin approval before publish. | P10 | P05, P09, P11, P12 | P10-US4, P10-US5, P10-US8, P12-US3 | H1 |

### Documents, capture, and migration intake

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-022** | Document upload, classification, and policy validation | A worker or API client uploads a classified case document; type/size rules, hash, metadata, storage, association, and audit are handled as one operation. | P03 | P04, P05, P09, P11, P13 | P03-US1, P03-US4, P04-US2, P05-US5 | H0 |
| **FM-023** | Durable document versioning | Workers add a current version without overwriting prior content, and the transition is historically explainable. | P03 | P08, P09, P11 | P03-US2 | H0 |
| **FM-024** | Required-document transition guards | Administrators configure document requirements that deterministically block workflow progress until qualifying documents exist. | P03 | P02, P05, P10 | P03-US5 | H0 |
| **FM-025** | Document protection, redaction, and locking | Authorized users restrict access, create versioned redactions, and lock filed documents while preserving original history and auditability. | P03 | P08, P09, P11 | P03-US6, P03-US8, P11-US7 | H2 |
| **FM-026** | Import mapping, dry run, and reconciliation | Operators import approved CSV/JSON/JSONL packages through validated versioned mappings, preview effects, commit accepted records, and reconcile results. Excludes a source-specific connector. | P14 | P01, P03, P04, P05, P09, P13 | P04-US3, P14-US2, P14-US3 | H1 |
| **FM-027** | Ingestion-channel governance and quarantine | Administrators enable tenant channels and validation policies; malformed items are isolated per record and operators can inspect, release, or reject them. | P04 | P08, P09, P10, P11, P14 | P04-US4, P04-US5, P14-US4 | H1 |
| **FM-028** | Inbound email capture | Approved email messages become documents or case notes with channel and message provenance. | P04 | P01, P03, P05, P09, P14 | P04-US6 | H3 |
| **FM-029** | Scan capture and safety quarantine | Scanned material enters quarantine, retains device/source provenance, and is released only after the configured scan result. | P04 | P03, P09, P14 | P04-US7 | H3 / Gated |
| **FM-030** | Inbound webhook commands | Authorized external webhooks submit validated events that become explicit case commands or state changes with idempotency and provenance. This is inbound and distinct from outbound event delivery. | P04 | P01, P09, P11, P13, P14 | P04-US8 | H3 |

### Search and forms

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-031** | Authorized structured case search | Workers filter cases by core and configured searchable fields; result sets and snippets enforce tenant, resource, and sensitive-field authorization. | P06 | P01, P05, P09, P11 | P05-US4, P06-US1, P06-US2, P06-US4 | H1 |
| **FM-032** | Saved and auditable searches | Workers save and reuse sorted searches while security staff can inspect query shape, timestamp, and result-count audit records. | P06 | P09, P11 | P06-US3, P06-US5, P06-US6 | H2 |
| **FM-033** | Authorized in-case document search | Workers find case documents using metadata and full text without obtaining visibility they would lack through direct document access. | P06 | P03, P05, P11 | P03-US3, P06-US8 | H2 |
| **FM-034** | Explainable semantic search | Workers receive authorization-filtered AI-ranked results with confidence and provenance cues; deterministic authorization remains authoritative. | P06 | P05, P11 | P06-US7 | H3 |
| **FM-035** | Bounded break-glass access | An authorized security workflow grants time-bounded, reason-bearing exceptional access, including cross-tenant auditor search, and prominently flags every resulting action. | P11 | P06, P09 | P06-US9, P11-US10 | H2 |
| **FM-036** | Versioned conditional case forms | Intake users submit a published case-bound form; administrators define sections, conditions, and immutable case-type bindings while hidden-field behavior preserves data safely. | P07 | P01, P05, P09, P10 | P07-US1, P07-US2, P07-US3, P07-US4 | H1 |
| **FM-037** | Visual form designer | Administrators visually author the same validated form-definition format used by runtime consumers. | P07 | P05, P10 | P07-US5 | H3 |
| **FM-038** | Advanced form execution | Workers complete resumable multi-step forms and state-specific workflow forms with explicit progress and transition validation. | P07 | P01, P02, P05, P10 | P07-US6, P07-US7 | H3 |

### Records, audit evidence, and security administration

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-039** | Legal-hold lifecycle | Security administrators place and release an audited hold that prevents disposition of the case, its documents, and associated audit evidence. | P08 | P01, P03, P09, P11 | P08-US1, P08-US2 | H1 (refusal seam in H0) |
| **FM-040** | Versioned retention and reviewed disposition | Records administrators publish retention policies by case type, document class, and audit category, then route eligible cases through an approval/rejection disposition workflow. | P08 | P01, P02, P03, P09, P10, P11 | P03-US7, P08-US3, P08-US4 | H2 |
| **FM-041** | Archival and disposition automation | Records administrators move cases to archival tiers, freeze disposition windows, and receive reviewable orphan-record proposals without silent deletion. | P08 | P01, P03, P09, P11 | P08-US5, P08-US7, P08-US8 | H3 |
| **FM-042** | Defensible-deletion evidence report | Compliance users obtain a chain-of-custody report showing what was disposed, under which policy version, by whom, and when. | P08 | P09, P12 | P08-US6, P12-US9 | H3 |
| **FM-043** | Case audit timeline and filtering | Authorized users and auditors browse a chronological, paginated case timeline and filter it by actor, type, date, and case without exposing redacted payloads. | P09 | P01, P11 | P09-US1, P09-US4 | H0 |
| **FM-044** | Audit export and corrective annotations | Authorized administrators export structured audit evidence and append reasoned corrections without altering the original event; each action is itself audited. | P09 | P08, P11 | P09-US5, P09-US7 | H2 |
| **FM-045** | Access-decision audit and reporting | Security staff see successful and denied access to sensitive resources, including the evaluated action, resource, actor, reason, and reportable result. | P09 | P11, P12 | P09-US6, P11-US9, P12-US5 | H2 |
| **FM-046** | Externally verifiable audit evidence | Operators stream persisted audit facts to a SIEM without weakening the durable chain, and auditors can request independently verifiable signed batches. | P09 | P11, P13 | P09-US8, P09-US9 | H3 / Gated |
| **FM-047** | Versioned extension and event-type registration | Developers register validated policy actions and event types as versioned modules; registration and tenant-defined audit types remain governed and auditable. | P13 | P09, P10, P11 | P09-US10, P13-US12 | H3 |
| **FM-048** | Role and permission administration | Security administrators define roles, assign permissions and users, and observe revocation without weakening deny-by-default enforcement. | P11 | P09, P10 | P11-US4 | H1 |
| **FM-049** | Session and trusted-client controls | Security administrators enforce idle/session limits, failed-attempt policy, and approved-client-certificate requirements for privileged actions. | P11 | P09 | P11-US8, P11-US11 | H3 / Gated |
| **FM-050** | Human-authoritative AI action boundary | AI may recommend but never authorize consequential work; the accountable human decision and identity remain distinguishable and audited. | P11 | P02, P05, P09, P12 | P11-US12 | H3 |

### Reporting, events, and connector lifecycle

| ID | Canonical feature | Shippable outcome and boundary | Primary | Supporting | Source stories | Horizon |
|---|---|---|---|---|---|---|
| **FM-051** | Operational case and task reports | Supervisors view source-timestamped open-case and overdue-task reports scoped to their teams and current authorization. | P12 | P01, P02, P09, P11 | P12-US1, P12-US2 | H2 |
| **FM-052** | Failed-work and outbox operations | Operators inspect retry count, last safe error, dead-letter state, and outbox lag, then safely replay eligible background work. | P13 | P09, P12 | P12-US4, P13-US9 | H1 |
| **FM-053** | Governed report export and custom definitions | Authorized users export reports with an audit trail, and administrators define controlled groupings, aggregations, filters, and time windows. | P12 | P05, P09, P10, P11 | P12-US6, P12-US7 | H3 |
| **FM-054** | Fresh operational dashboards | Managers view authorization-aware operational KPIs with explicit source time and sub-five-second freshness. | P12 | P01, P02, P03, P05, P09, P11, P13 | P12-US8 | H3 |
| **FM-055** | Versioned event catalog and reliable delivery | External consumers subscribe to documented, backward-compatible event versions delivered through the configured message-bus adapter. | P13 | P01, P02, P03, P09, P11 | P13-US8 | H1 |
| **FM-056** | Authenticated outbound webhooks | Authorized subscribers receive authenticated, retry-safe outbound domain-event deliveries. This is outbound and distinct from inbound webhook commands. | P13 | P09, P11, P14 | P13-US10 | H3 |
| **FM-057** | Governed GraphQL query surface | Authorized API consumers perform supported ad-hoc queries without bypassing service, tenant, field, or document policy boundaries. | P13 | P01, P03, P05, P11 | P13-US11 | H3 |
| **FM-058** | Approved connector lifecycle | Security administrators approve, enable, monitor, retire, and safely drain an enterprise connector. Implementing any named connector requires separate explicit authorization. | P14 | P04, P09, P11, P13 | P14-US5, P14-US7 | H3 / Gated |
| **FM-059** | Source coexistence and migration state | Administrators define a truthful coexistence policy with source references, ownership, provenance, and migration state without implying unapproved real-time synchronization. | P14 | P01, P09, P10, P13 | P14-US6 | H3 / Gated |
| **FM-060** | Portable case evidence bundle | Operators export an authorized portable archive containing case data, documents, and audit history for offline review or legal-hold use. | P14 | P01, P03, P08, P09, P11 | P14-US8 | H3 |

## 3. Dependency spine

The table is a catalog, not a promise that features can be pulled in arbitrary order. The minimum dependency spine is:

```text
FM-001 Local-safe runtime
  ├─ FM-002 Authorization and tenant isolation
  ├─ FM-003 Atomic audit foundation
  └─ FM-004 API/client contract
       └─ FM-008 Case-type schema publication
            └─ FM-005 Case intake
                 ├─ FM-006 Case workspace
                 │    └─ FM-007 Safe case editing
                 ├─ FM-013 Workflow definition lifecycle
                 │    └─ FM-014 Task completion and transition
                 │         ├─ FM-016 Queues
                 │         ├─ FM-017 Approvals
                 │         └─ FM-018 SLA escalation
                 └─ FM-022 Document upload
                      ├─ FM-023 Document versioning
                      └─ FM-024 Required-document guards
```

Major later dependencies:

- **Search**: FM-031 requires FM-002, FM-006, FM-008, and FM-011. FM-033 additionally requires FM-022 and FM-025.
- **Forms**: FM-036 requires FM-005, FM-008, and the configuration-governance rules in FM-021.
- **Import**: FM-026 requires FM-005 and FM-022; quarantine behavior comes from FM-027.
- **Records**: FM-040 requires FM-003, FM-021, FM-023, FM-039, and approval behavior from FM-017.
- **Reporting**: FM-051 requires stable case/task read models; FM-053 also requires FM-045-style access auditing.
- **Connectors**: FM-058 and FM-059 require FM-026, FM-052, and FM-055, plus explicit authorization and an available enterprise environment.

## 4. Recommended pull order after the first slice

After `001-case-management-platform` converges, pull H1 features in this order unless a concrete business-process pilot changes the priority:

1. **FM-048 Role and permission administration** — makes the security foundation operable.
2. **FM-011 Sensitive case and field controls** — closes a major security boundary before expanding retrieval.
3. **FM-021 Configuration governance and publication history** — stabilizes all later administrator-authored artifacts.
4. **FM-031 Authorized structured case search** — delivers immediate worker value on top of secure metadata.
5. **FM-016 Queue administration and worker distribution** — makes human work operational at team scale.
6. **FM-017 Approval transitions and separation of duties** — unlocks controlled consequential workflows.
7. **FM-052 Failed-work and outbox operations** and **FM-055 Event catalog and reliable delivery** — harden asynchronous operation before integrations.
8. Choose the next vertical according to the pilot: **FM-036 Forms**, **FM-026/027 Import**, or **FM-039/040 Records**.

Do not start all H1 rows concurrently. The map is a decision inventory; the active roadmap should still contain only a small number of selected slices.

## 5. Deduplication decisions

The most significant merges are:

| Canonical feature | Duplicate descriptions merged |
|---|---|
| FM-005 Case intake | Case creation (P01), API ingestion (P04), schema validation (P05), and idempotent API behavior (P13) |
| FM-013 Workflow lifecycle | Workflow semantics (P02) and configuration publication (P10) |
| FM-017 Approval transitions | Workflow approval (P02) and security separation-of-duties policy (P11) |
| FM-022 Document upload | Document persistence (P03), upload capture (P04), classification (P05), and audit/security cross-cuts |
| FM-026 Import pipeline | Capture dry-run (P04) and migration mapping/reconciliation (P14) |
| FM-031 Structured search | Searchable metadata declaration (P05) and authorized retrieval behavior (P06) |
| FM-040 Retention and disposition | Document-class retention (P03) and records policy/disposition (P08) |
| FM-045 Access reporting | Audit evidence (P09), authorization decisions (P11), and reporting surface (P12) |
| FM-052 Failed-work operations | Operational failure report (P12) and outbox/replay controls (P13) |

Inbound webhook commands (FM-030) and outbound webhook event delivery (FM-056) remain separate because they have different trust direction, actors, failure semantics, and acceptance tests. Likewise, general API conventions (FM-004) remain shared infrastructure while business endpoints stay inside their owning feature.

## 6. Traceability and change control

- Every current `PXX-USN` story appears exactly once in the catalog's **Source stories** cells.
- A new pillar story must either map to an existing feature or create a new canonical row; it must not silently duplicate another pillar's outcome.
- When a feature spec is created, replace its horizon with the concrete `NNN-short-name` link while preserving the source-story references.
- If a feature is split, each source story and acceptance scenario must move to exactly one child feature, with shared constraints recorded as cross-cuts rather than copied user outcomes.
- Open questions remain in their pillar files until a selected feature resolves them through `/speckit-clarify`; resolution should then be linked back to the pillar.
