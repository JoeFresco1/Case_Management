# PRD — Remaining Pillars (H1 / H2 / H3 / Gated)

**Status**: Agreed scope for Orca orchestration
**Owner**: Platform team
**Date**: 2026-09-11
**Sources**: `specs/002-leftovers/spec.md` (authoritative umbrella — 8 tracks T1–T8),
`specs/002-leftovers/plan.md` (Run split A–H), `specs/pillars/01-deduplicated-feature-map.md` (pull order §4),
`specs/001-case-management-platform/first-slice/tasks.md`
**Recon** (outside repo): `C:\Users\Fresc\case_management_feature_recon\output\onbase_features.json` (589),
`onbase_taxonomy.json` (21 pillars), `onbase_replacement_matrix.md`, `onbase_user_journeys.md` (18 journeys),
`onbase_unknowns.md` (34). Hyland terms appear only as `Migration source:` traceability, never as domain names (Constitution XII).
**Constitution**: `.specify/memory/constitution.md` — highest authority on any conflict

## 1. Goal

Converge every pillar capability not shipped in `001-case-management-platform` (first slice, H0),
in dependency order, as a sequence of Orca orchestration Runs (one Run = one DAG = one reviewable slice).
Each Run delivers independently testable P1/P2 user stories with automated acceptance tests.

## 2. Starting state (verified 2026-09-11)

- First slice (H0: FM-001–008, FM-013–015, FM-022–024, FM-043 + foundations) is converged and committed (`783ba4a` and predecessors).
- The working tree is **dirty with uncommitted H1–H3 scaffolding**: search, forms, imports, queues, break-glass,
  outbound webhooks, legal holds, reference formats, case relationships, `domain/ports/` package split
  (replacing deleted `domain/ports.py`), alembic `0020–0027`. None of it is reviewed, gated, or committed.
- Therefore Run 0 stabilizes before any new feature work.

## 3. MVP scope — phased Runs (strict sequence, max 2 H1 features in flight, maxConcurrency=4)

Track plan per `specs/002-leftovers/plan.md §1`. Each Run carries 002 acceptance (FR-L01–L08, SC-L01–L07)
plus recon traceability: every task maps its output to `onbase_features.json` ids and the governing journey.

| Run | 002 track(s) | Outcome |
|---|---|---|
| **Run 0 — Stabilize** | dirty-tree triage (incl. untracked `specs/002-leftovers/`, `docs/decisions/ADR-030–035`) | Gates green; scaffolding split into reviewed slice commits (proposed, user-approved); truthful inventory of what is real vs stub |
| **Run A — security** | 002 §T4-gov/T7 partial: FM-048 roles, FM-011 sensitive-field controls, FM-021 governance | Security foundation operable; redaction boundary closed; admin artifacts governed |
| **Run B — worker value** | T4-ops + T2-search: FM-031 search (+FM-011 snippet redaction), FM-016 queues, FM-017 approvals/SoD, FM-018 SLA | Worker value on secure metadata; team-scale distribution; consequential transitions gated |
| **Run C — content depth** | T5: FM-025 protection/redaction/lock, viewer pipeline (paged/thumbnail/detail, annotations, renditions, checkout history, edit-locking, re-index), doc ops (x-refs, separation workspace, packages, hash-dedup, CSV export), worker UX (home, favorites, shortcuts, grouping, task pane, recents, preferences), collaboration bundles + share-by-link | Document + worker ergonomics without cloned ribbons |
| **Run D — capture** | T1: FM-026 import packages + dry-run/commit/reconciliation, FM-027 channels/quarantine, watched-folder, report-stream ingestion, assisted indexing (barcode/OCR/zones/ICR/AutoFill seams), scan workspace, capture-to-case routing | Intake parity with per-record quarantine, never silent loss |
| **Run E — structured content** | T3 + T2-meta: FM-036 forms runtime, FM-038 advanced execution, FM-037 designer; field flags/defaults/Save-As/note-types, FM-012 derived fields | Forms + metadata depth |
| **Run F — governance depth** | T6 + T4-model: FM-039 hold, FM-040 retention/disposition, FM-041 archival, FM-042 evidence, FM-044 export/annotations, FM-045 access reporting, FM-051 reports, FM-009 refs, FM-010 relationships | Records + case-model depth |
| **Run G — operability** | T7: FM-052 outbox ops, FM-055 event catalog, FM-056 webhooks, FM-057 governed queries, FM-047 extensions, diagnostics/logging/forwarding utilities, storage-admin abstraction, T5/T6 reporting depth (FM-053/054) | Operable + developer surface |
| **Run H — Gated** | T8: FM-058 connectors (each named connector separately authorized), FM-059 coexistence, FM-060 bundles, FM-028/029/030 channels, FM-034 semantic (bounded), FM-019/020 advanced workflow + designer, FM-049 sessions/certs, FM-046 SIEM/signed, FM-050 AI boundary | Only with explicit authorization + available enterprise environment |

Open questions from 002 §9 resolved by gates, not guesses: pilot track (T1 vs T2), named-connector approvals,
SOAP compat, offline conflict semantics, retention-clock pause/resume, full-text engine adapter.

Pull order follows feature-map §4. Do not open duplicate specs for H0 rows. One primary pillar per slice; supporting pillars contribute acceptance criteria only.

## 4. Explicit non-goals

- No live Azure calls, no `az login`, no Entra tenant — Azure adapters remain contract-only seams with local adapters active (`AGENTS.md` §3, §12).
- No Docker Desktop / healthy-WSL assumption — sqlite test profile is the gate profile; postgres path is best-effort.
- No Gated feature (Run G) without explicit user authorization per feature.
- No AI-authoritative decisions — `recommend()` verb only, human remains authoritative (Constitution XIV).
- No big-bang migration framing — vertical slices only (Constitution XI, XIII).
- No commits without user approval at the per-Run gate (commit policy).

## 5. Done per Run (gates)

`ruff check` + `ruff format --check` + `mypy --strict app/domain app/application` + `import-linter verify` +
`APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q` (cov ≥80% domain+application) +
`pnpm typecheck && pnpm build` + tenant-isolation test per repo/query + `architecture.md` (Mermaid) + ADR entries +
OpenAPI regen where API changed. Each Run ends with an approval gate before slice commits land.
