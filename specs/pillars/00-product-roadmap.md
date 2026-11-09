# Product Roadmap

**Status**: Draft
**Owner**: Platform team
**Last updated**: 2026-08-31

This document is the top of the product spec tree. It enumerates the 14 product pillars of the Case Management Platform, defines scope discipline (what we are and are not building), establishes pillar cross-cuts (which pillars depend on which), and tracks feature-priority at the product level. Each pillar has its own `pillar.md` in `specs/pillars/PXX-*/`.

The cross-pillar, deduplicated inventory of candidate feature slices is maintained in [`01-deduplicated-feature-map.md`](./01-deduplicated-feature-map.md). Use that map to select the unit for a Spec Kit lifecycle; do not run Spec Kit once per pillar.

The 14 pillars were chosen to align with **the operational capabilities that enterprise case-management and content platforms must provide**, while deliberately avoiding the structural shape of any single legacy product (Constitution XII).

---

## 1. The 14 pillars

| # | Pillar | One-line statement | First-slice scope |
|---|---|---|---|
| P01 | **Case & Business Object Management** | A Case is the primary unit of work. The platform models business objects generically and treats each as a first-class entity. | **In first slice.** |
| P02 | **Workflow, Tasks & Queues** | Work moves through explicit, versioned, deterministic workflow. Tasks and queues are first-class. | **In first slice (Phase 2/3).** |
| P03 | **Document & Content Management** | Documents are first-class objects associated with cases. Storage is abstracted behind a port. | **In first slice (Phase 2).** |
| P04 | **Capture & Ingestion** | Inbound material (upload, scan, email, API, import) becomes a case, a document, or a participant, with provenance. | Partial in first slice (upload only). |
| P05 | **Metadata & Indexing** | Structured and unstructured metadata is defined, validated, and made queryable in a controlled way. | **In first slice (via case schema).** |
| P06 | **Search & Retrieval** | Authorized users can find cases and documents using controlled, auditable search. | Deferred (post first slice). |
| P07 | **Forms** | Dynamic data collection, both for case creation and for in-case data entry, with versioning. | Deferred. |
| P08 | **Records & Retention** | Records-management discipline: legal hold, retention, archival, defensible disposition, all auditable. | Deferred (P08 metadata in first slice). |
| P09 | **Audit, History & Compliance** | Every meaningful action is explainable, durable, and tamper-evident. | **In first slice.** |
| P10 | **Administration & Configuration** | Configuration is versioned product data, not code. Admins can design processes without engineering. | **In first slice (Phase 3).** |
| P11 | **Security & Identity** | Least privilege, deny by default, server-side enforcement, separation of duties, identity-portable. | **In first slice (P11 foundation only).** |
| P12 | **Reporting & Analytics** | Operational truth: reports that reflect current and historical state, distinguishable from each other. | Deferred (P12 surface in first slice is just `/health`). |
| P13 | **API, Events & Extensibility** | Versioned APIs, documented events, extension points for future modules. | **In first slice (API foundation).** |
| P14 | **Integrations & Migration** | Connectors to enterprise systems; incremental migration from legacy platforms. | Deferred. |

> **First slice = pillars P01, P02 (partial), P03 (partial), P05, P09, P10 (partial), P11 (foundation), P13 (foundation).** Other pillars are documented in their pillar.md but not built until a later feature slice.

---

## 2. Scope discipline

### 2.1 What we are building

- A modular monolith with explicit module boundaries and ports/adapters (Constitution XV, VI).
- A case-centric, audit-grade, configuration-driven platform.
- Local-first development; Azure-targeted production (Constitution VI, XII).
- Multi-tenant from day one (Constitution V).
- Declarative, versioned, immutable published configuration (Constitution III, IX).
- Deterministic core; AI only as bounded assistance later (Constitution XIV).

### 2.2 What we are explicitly not building (yet)

- A clone of OnBase (or any other legacy product) in terminology, structure, or behavior (Constitution XII).
- Real-time human-chat, social, or collaboration surfaces.
- A native mobile client.
- An AI model that can make unreviewed consequential decisions (Constitution XIV).
- An OnBase, Panzura, or any enterprise connector in the first slice (Constitution V.10, XII.5).
- A production Azure deployment in the first slice.
- E-signature, e-fax, real document scanning in the first slice.
- A complete authoring UI for configuration (P10) — configuration ships as JSON files in the first slice; the admin UI lands in a later slice.
- Multi-region, autoscaling, or HA beyond what the first slice's test profile needs.

### 2.3 What we will not build at all (negative scope)

- "Smart workflow" that auto-completes consequential work via LLM.
- Direct database access by external systems (P13 enforces API/Events only).
- Configuration that runs arbitrary unreviewed code in the admin path (Constitution IX).
- Any persistence of unrestricted PHI in development or test fixtures (Constitution V.10, XIII.1).

---

## 3. Pillar cross-cuts (dependencies)

Most pillars depend on others. The map below is the authoritative view of pillar-to-pillar dependencies. A pillar in the "depends on" column must reach a stable version before the pillar in the "pillar" column can publish its first shippable version.

| Pillar | Depends on (for first shippable version) |
|---|---|
| P01 Case & Business Object Management | P05 (metadata), P09 (audit), P11 (identity), P13 (API) |
| P02 Workflow, Tasks & Queues | P01, P05, P09, P10 (workflow definitions live in P10) |
| P03 Document & Content Management | P01 (case), P05 (metadata), P09 (audit), P11 (access control) |
| P04 Capture & Ingestion | P01, P03, P05, P09, P13 (events) |
| P05 Metadata & Indexing | (foundational) |
| P06 Search & Retrieval | P01, P03, P05, P11 (authorization-filtered search) |
| P07 Forms | P01, P05, P10 (form definitions) |
| P08 Records & Retention | P01, P09, P11 |
| P09 Audit, History & Compliance | (foundational) |
| P10 Administration & Configuration | P05, P09, P11 |
| P11 Security & Identity | (foundational) |
| P12 Reporting & Analytics | P01, P02, P03, P05, P09 |
| P13 API, Events & Extensibility | (foundational) |
| P14 Integrations & Migration | P01, P03, P05, P09, P13 |

Foundational pillars (P05, P09, P11, P13) are present in the first slice as minimal versions. They grow with every subsequent slice.

---

## 4. Feature-priority scheme

Each pillar uses a three-tier priority scheme, applied consistently:

- **P1 (Critical)**: Must be in the first shippable slice. Without it, the platform does not deliver a vertical value.
- **P2 (Important)**: Should be in a near-term slice. Adds depth or breadth that users notice.
- **P3 (Deferred)**: Not in the first few slices. Documented in the pillar; planned for a specific later feature.

Within a pillar, a P1 user story is **independently testable** (Constitution XVI). It can be developed, tested, deployed, and demonstrated on its own. This is the contract that lets a pillar ship in slices rather than all at once.

---

## 5. Feature slice lifecycle

A pillar publishes functionality through **feature slices**, each a separate folder under `specs/`. The current first slice is `001-case-management-platform`. Future slices follow `NNN-<short-name>/`.

A feature slice is done when:

- The pillar's P1 user stories pass.
- The plan and tasks documents for that slice are complete.
- The walking-skeleton checkpoint (if any) has been exercised on a clean checkout.
- The acceptance scenarios in the slice's spec are automated or demonstrable.

---

## 6. How to use this document

- **To select the next feature**: use `01-deduplicated-feature-map.md` to choose one canonical, dependency-ready slice. A feature has one primary pillar and may draw requirements from several supporting pillars.
- **To add a new feature**: identify the primary pillar, check the cross-cuts, draft a feature slice under `specs/NNN-name/`, and link it from the pillar's `pillar.md` "Planned slices" section.
- **To revise a pillar**: update the pillar's `pillar.md`. If the change is constitutional, also update `.specify/memory/constitution.md`.
- **To resolve a conflict between pillars**: the cross-cut map wins. A pillar cannot depend on functionality another pillar has not yet committed to.

---

## 7. Open product questions

These are the questions that affect more than one pillar and have not been resolved in the constitution or in any feature slice. The full material-decisions index for the first slice lives in [`docs/decisions/ADR-020-final.md`](../../docs/decisions/ADR-020-final.md); each decided question below is linked to the ADR that resolved it. Per AGENTS.md §4.R8, every material decision that resolves a question on this list must appear here and in an ADR.

1. **Multi-tenant single-DB vs. database-per-tenant** — decided: single-DB with `tenant_id` everywhere. Database-per-tenant is a future scaling decision. See [ADR-021](../../docs/decisions/ADR-021-multi-tenant-persistence-strategy.md).
2. **Real-time event delivery** — decided: outbox is the source of truth; WebSockets/SSE deferred. See [ADR-002](../../docs/decisions/ADR-002-seams.md) §Outbox and [ADR-028](../../docs/decisions/ADR-028-event-transport-and-deferred-scope.md).
3. **Notifications** — first slice records communication *intents*; outbound channels (email, SMS, fax, portal) are P14 territory. **Open** — no ADR yet; tracked for P14.
4. **AI assistance surface** — decided: single `recommend()` verb (`AIAdvisor`), no-op local stub, Azure OpenAI placeholder; never authoritative. See [ADR-012](../../docs/decisions/ADR-012-ai-boundary.md).
5. **Public API vs. internal API** — decided: internal API only in the first slice; public API comes with hardening. See [ADR-022](../../docs/decisions/ADR-022-public-vs-internal-api.md).
6. **Review remediation boundaries** — decided: persisted local identity and live role authority, exact idempotency exceptions, redacted search audit shapes, and durable webhook enqueue retry. See [ADR-048](../../docs/decisions/ADR-048-review-remediation-boundaries.md).
