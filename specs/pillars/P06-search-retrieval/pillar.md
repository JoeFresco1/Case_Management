# Pillar P06 — Search & Retrieval

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Authorized users can find cases and documents using controlled, auditable search.

## 1. Pillar statement

This pillar owns the **search experience**: how authorized users find cases, documents, and other objects. Search is derived from authorized source data; the search index is **never** the source of truth. Authorization is enforced at search-result time, not just at result-display time (Constitution V; "search indexing must not become an access-control bypass").

Search supports controlled, declarative query shapes: case identifiers, case type, status, queue, assignee, dates, participant references, sensitivity labels, and configured searchable fields (P05). Full-text search is configurable per field, per case type.

The pillar is the integration point for any future search engine (PostgreSQL FTS, Azure Cognitive Search, OpenSearch). The first slice uses a portable `LIKE`-based adapter; richer engines are swap-in replacements behind the same port.

## 2. Scope

### In scope
- Case search by case reference, case type, status, queue, assignee, priority, due date, sensitivity, participant reference.
- Fielded filtering on searchable fields (P05).
- Search-result authorization: results are filtered to cases the actor can see.
- Search-result audit: who searched for what, when, with which result count.
- A `SearchProvider` port so the engine is pluggable.
- Per-tenant result-size limits and pagination.

### Out of scope (deferred)
- AI-assisted semantic search (P3, gated by Constitution XIV).
- Federated search across external systems.
- Saved searches (per-actor) — P3.
- Search across audit log content — P3; P09 owns the audit index.

### Explicitly not building
- A search index that contains data the actor is not authorized to see, even temporarily. The index shape is enforced at write time, not just at query time.
- A "search the world" experience. Search is bounded by the actor's authorization context.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `SearchProvider` (port) | The search engine abstraction. | Yes (LIKE adapter) |
| `SearchQuery` | A controlled query shape (filters, sorts, limit, cursor). | Yes |
| `SearchResult` | An ordered set of references (case ids) with snippets. | Yes |
| `SearchAuditEvent` | An audit event for search activity. | Deferred |
| `SavedSearch` | A named, reusable query. | Deferred |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P06-US1**: As a case worker, I can search for cases by case reference, case type, status, queue, assignee, priority, and due date; results respect my authorization scope.
- **P06-US2**: As a case worker, I can search for cases by a configured searchable field (e.g., `case.data.participantName`); sensitive field values are redacted in result snippets.

### P2 — Important
- **P06-US3**: As a case worker, I can save a search (filtered + sorted) and reuse it.
- **P06-US4**: As a process administrator, I can configure which fields are searchable for a case type; non-searchable fields are not returned in snippets.
- **P06-US5**: As a security administrator, I can inspect search activity for a user and see the queries, timestamps, and result counts.
- **P06-US6**: As a system, every search writes an audit event with the actor, query shape, and result count.

### P3 — Deferred
- **P06-US7**: As a case worker, I can run a semantic search and see AI-ranked results with confidence labels; the result set is still filtered to what I can see.
- **P06-US8**: As a case worker, I can search across documents by content (full-text) within a case.
- **P06-US9**: As an operator, I can configure cross-tenant search for auditors and the system requires a break-glass reason.

## 5. Cross-cuts

- **P01 (Case)**: search returns cases; result set is filtered to visible cases.
- **P03 (Document)**: per-case document search is a sub-feature; document content search is P3.
- **P05 (Metadata)**: searchable fields are declared in the case type schema.
- **P09 (Audit)**: search activity is audited.
- **P11 (Security)**: authorization is the primary filter on results.
- **P12 (Reporting)**: aggregate search is a P12 concern; this pillar is per-actor interactive search.

## 6. Acceptance scenarios (representative)

- F1: A case worker searches for cases with status `in_progress` and queue `review`. Results are limited to cases in their tenant and queue; pagination works.
- F2: A user without permission to view a sensitive case runs a search that would otherwise match it. The case does not appear in results.
- F3: A case worker searches for a sensitive field value. The result is included (the case is visible), but the field value is redacted in the snippet.
- F4: A case worker runs the same search 100 times in 1 second. The system enforces a per-actor rate limit; the actor is not silently blocked but the request returns `rate_limited`.
- F5: A process administrator marks a previously searchable field as non-searchable. New searches do not return the field in snippets; existing indexes are rebuilt to remove the field (or the engine is configured to ignore it).

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements a basic case list endpoint (`GET /cases`) that respects tenant and status filters. Full search UI in a later slice. |
| `002-leftovers` Run B | Draft | Structured search (FM-031 / P06-US1, US2), saved searches + search audit (FM-032 / P06-US3, US5, US6), in-case document search (FM-033), AI-ranked semantic search as bounded assistance (FM-034). Portable LIKE adapter; engine swap is a same-contract seam. See `specs/002-leftovers/architecture-runB.md`, `docs/decisions/ADR-038-runB-like-search-and-sla-semantics.md`. |
| Future slice 2 | Planned | P06-US1, US2 — declarative search with fielded filters, redaction in snippets. |
| Future slice 3 | Planned | Saved searches, full-text within a case, semantic search. |

## 8. Open questions

- Q1: What is the maximum result-page size and the default? (Current intent: 50 default, 200 max.)
- Q2: Are case-list operations (`GET /cases?status=...`) "search" or "list"? (Current intent: they are list operations; "search" implies free-text or complex criteria. The boundary is configurable in P5.)
- Q3: Is search activity auditable per query, or per result view? (Current intent: per query; per-result audit would be too noisy.)
