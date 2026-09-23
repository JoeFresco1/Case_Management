# Orchestration Roadmap — Runs 0, A–G (ready-to-paste Orca commands)

**PRD**: [`PRD-remaining-pillars.md`](./PRD-remaining-pillars.md) · **Map**: `specs/pillars/01-deduplicated-feature-map.md`
**Viewer**: `npx orca-dag` → pick Run in top bar → harness per node → "Run with Orca"
**CLI from source** (installed `orca.cmd` shim is broken — `orca.exe` missing from `resources/bin`):
`node out/cli/index.js <args>` with cwd `C:\Users\Fresc\orca-main` (built 2026-09-11 via direct `tsc`; do NOT `pnpm run build:cli` — pnpm patch step fails on `@vscode__windows-process-tree`).
**Coordinator target**: run all commands with cwd `C:\Users\Fresc\case_management` — orchestration binds per-terminal, so open a fresh terminal for each new Run (`run-create` binds that terminal as coordinator).

Rules: one Run = one DAG; never `reset`; tasks cannot be edited after creation (get specs right first pass);
`task-create --deps` takes a JSON array of same-Run task ids; verify with `task-list --run <id>`;
gates block their task until resolved in the viewer; max 2 H1 features in flight; tasks touch ≤3 files + tests.

---

## Run 0 — Stabilize the dirty tree (CREATED 2026-09-11: `run_bca98410fcfc`)

Objective: `Run 0: stabilize uncommitted H1-H3 scaffolding into gated, reviewed slice state`
Tasks: T0-1 `task_5378ce41bcbb`, T0-2 `task_c6896631701f`, T0-3 `task_1596b592861e`,
T0-4 `task_8ecd03f2b07b` (roots) → T0-5 `task_d8bf33f9cd8c` (deps all four) + gate `gate_ed0d8216fbf0`,
T0-6 `task_0e0a3d7c0919` (dep T0-1). Graph verified via `task-list`.
Original commands (ids captured above; tasks immutable — to redraw, open a new Run, never `reset`):

```bash
node out/cli/index.js orchestration run-create --objective "Run 0: stabilize uncommitted H1-H3 scaffolding into gated, reviewed slice state" --json
# save result.run.id as RUN0
```

Root tasks (no deps) — run in parallel (4-way):

```bash
# T0-1 — baseline gates report (read-only)
node out/cli/index.js orchestration task-create --task-title "T0-1 baseline gates report" --spec "Inputs: read backend/pyproject.toml, AGENTS.md sections 9 and 15. Outputs: no source changes; report gate results only. Acceptance: run uv run ruff check ., uv run ruff format --check ., uv run mypy --strict app/domain app/application, uv run lint-imports verify, and APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q; paste each command result verbatim into the task result." --json
# T0-2 — ports split review
node out/cli/index.js orchestration task-create --task-title "T0-2 verify domain ports package split" --spec "Inputs: read backend/app/domain/ports/ package, git show HEAD:backend/app/domain/ports.py, backend/pyproject.toml import-linter contracts. Outputs: no source changes unless a cycle exists; report import graph. Acceptance: import-linter verify passes and no module imports app.interfaces or app.infrastructure from app.domain or app.application; list every Protocol in domain/ports with its local adapter." --json
# T0-3 — migrations 0020-0027 audit
node out/cli/index.js orchestration task-create --task-title "T0-3 audit migrations 0020-0027" --spec "Inputs: read backend/alembic/versions/0020_queue_version_token.py through 0027_import_mapping_and_form_binding_lifecycle.py. Outputs: no source changes; report per migration. Acceptance: for each migration state upgrade AND downgrade presence, portable generic types only (no JSONB/GIN/ILIKE/tsvector/gen_random_uuid), tenant_id FKs indexed where applicable; list violations file:line." --json
# T0-4 — scaffolding inventory
node out/cli/index.js orchestration task-create --task-title "T0-4 inventory uncommitted scaffolding by FM" --spec "Inputs: run git status --short and git diff --stat in C:/Users/Fresc/case_management; read specs/pillars/01-deduplicated-feature-map.md sections 2-4. Outputs: no source changes; mapping table. Acceptance: every modified/untracked path mapped to exactly one FM-0xx (or marked stray); stubs and TODOs listed with file:line; result posted as markdown table." --json
```

Dependent tasks (create after roots, `--deps` = the four ids above):

```bash
# T0-5 — slice-commit plan (depends on T0-1..T0-4)
node out/cli/index.js orchestration task-create --task-title "T0-5 slice-commit plan" --spec "Inputs: read results of T0-1 baseline gates, T0-2 ports review, T0-3 migration audit, T0-4 FM inventory. Outputs: no source changes; ordered commit plan. Acceptance: propose 4-8 slice commits (security/break-glass, search, forms, imports, queues, webhooks-events, relationships-refs, frontend), each listing exact files, owning FM, and gate status; flag anything that must be reverted rather than committed." --deps '["task_T01","task_T02","task_T03","task_T04"]' --json
# T0-6 — repair gate failures (depends on T0-1; parallelizable group, one task per failing gate area, max 3)
node out/cli/index.js orchestration task-create --task-title "T0-6 repair gate failures without new features" --spec "Inputs: read T0-1 gate failures and T0-5 plan. Outputs: minimal edits fixing lint/type/layer/test failures only. Acceptance: the failing gate command passes; no new endpoints, tables, or domain concepts added; ruff/mypy/import-linter deltas shown in result." --deps '["task_T01"]' --json
```

Gate (blocks T0-5):

```bash
node out/cli/index.js orchestration gate-create --task task_T05 --question "Approve slice-commit plan? Approving authorizes the user (not workers) to commit the proposed slices." --options '["approved","rejected"]' --json
```

Verify: `node out/cli/index.js orchestration task-list --run <RUN0> --json`

## Run A — H1 security/config (CREATE AFTER RUN 0 GATE APPROVED)

Objective: `Run A: FM-048 roles, FM-011 sensitive-field controls, FM-021 config governance`
Pillars: P11 primary (FM-048/011), P10 primary (FM-021); supporting P01/P05/P09/P13.

```bash
node out/cli/index.js orchestration run-create --objective "Run A: FM-048 roles, FM-011 sensitive-field controls, FM-021 config governance" --json
# Roots (parallel): A-1 FM-048 domain roles/permissions; A-2 FM-011 classification/redaction domain; A-3 FM-021 publication-history domain.
# Each spec: Inputs (pillar.md user stories + domain files), Outputs (domain module + unit tests), Acceptance (deny-by-default / redaction / immutability tests + mypy strict).
# Second layer (deps on domain): A-4 FM-048 admin API + tenant-isolation tests; A-5 FM-011 enforcement in responses/logs/events + tests; A-6 FM-021 history/approval API + tests.
# Third layer: A-7 frontend (roles admin, sensitivity badges, config history); A-8 architecture.md Mermaid + ADRs + OpenAPI regen + full gates.
# Gate on A-8: "Approve Run A slice commits and pillar Planned-slices links?"
```

## Runs A–H (ALL QUEUED 2026-09-12 — fire in order, one Run at a time in the viewer)

| Run | Orca id | Tasks | Entry gate | Exit gate |
|---|---|---|---|---|
| A security (FM-048/011/021) | `run_bec207fb8cd3` | 8 | — (live now) | `gate_6863a16fc341` |
| B worker value (search/queues/approvals/SLA) | `run_ac1db50bad45` | 8 | `gate_5fe6f47102cf` (A converged?) | `gate_35a75be48a68` |
| C content depth (T5 viewer/sharing) | `run_550a17d14997` | 9 | `gate_a75ca13abaed` (B converged?) | `gate_6a767f99f710` |
| D capture (T1) | `run_c1d2452f9f87` | 8 | `gate_076151bf03f9` (A converged?) | `gate_60c083321a94` |
| E forms + metadata (T3/T2-meta) | `run_403aee62931b` | 8 | `gate_6a19c0f3100d` (A converged?) | `gate_ce5756cc856a` |
| F governance (T6/T4-model) | `run_e4bce7c7f5f0` | 8 | `gate_02b2dee2ac49` (A+B converged?) | `gate_42f17303f93f` |
| G operability (T7) | `run_d93034f14d25` | 8 | `gate_ac0fda3bb746` (F converged?) | `gate_ed90cc5b8614` |
| H gated (T8 + trust) | `run_aaa761b57ec2` | 8 | `gate_232566a5d870` (G + connector auth?) | `gate_a4bbce0915f6` |

57 tasks queued. Task specs below were the build scripts; authoritative scope per Run is its 002 track (T1–T8).

Authoritative scope per Run is the 002 track section (T1–T8), NOT the FM shorthand below.
Every Run spec must include: (a) 002 user stories + acceptance for its track, (b) recon traceability —
each deliverable cites `onbase_features.json` ids + governing journey from `onbase_user_journeys.md`,
Hyland terms only as `Migration source:` lines, (c) new entities from 002 §5 for the track
(`Annotation, Redaction, CheckoutLock, DocBundle, EnvironmentValueSet`, etc.), (d) API routes from 002 plan §4.

- **Run A — security**: 002 T4-gov/T7 partial — FM-048, FM-011, FM-021. Pillars P11/P10 primary.
- **Run B — worker value**: 002 T4-ops + T2-search — FM-031 (+FM-011 snippets), FM-016, FM-017 (needs A), FM-018, FM-032/033/035. P06/P02 primary.
- **Run C — content depth (the previously-missing T5)**: 002 T5 — FM-025, viewer pipeline, doc ops, worker UX, collaboration bundles. P03 primary. Split into C1 (protection/redaction/lock + viewer) / C2 (doc ops + worker UX + sharing) if >12 tasks.
- **Run D — capture**: 002 T1 — FM-026, FM-027, watched-folder, report-stream, assisted-indexing seams, scan workspace, capture-to-case routing. P04/P14 primary.
- **Run E — structured content**: 002 T3 + T2-meta — FM-036, FM-038, FM-037, field flags/defaults/Save-As, FM-012. P07/P05 primary.
- **Run F — governance depth**: 002 T6 + T4-model — FM-039, FM-040, FM-041, FM-042, FM-044, FM-045, FM-051, FM-009, FM-010. P08/P09/P12/P01 primary. Records 002 §9 retention-clock decision as a gate.
- **Run G — operability**: 002 T7 — FM-052, FM-055, FM-056, FM-057, FM-047, diagnostics/logging utilities, storage-admin abstraction, FM-053/054. P13/P10 primary.
- **Run H — Gated**: 002 T8 — FM-058 (per-connector authorization), FM-059, FM-060, FM-028/029/030, FM-034 (bounded), FM-019/020, FM-049, FM-046, FM-050. One user authorization per feature before its tasks are created; 002 §9 SOAP/offline-conflict questions as gates.

Each Run follows the Run A shape: domain roots in parallel → application/infrastructure layer → interfaces/frontend → architecture+ADRs+gates, with a pre-commit approval gate on the final task.
Gates that resolve 002 §9 open questions (pilot T1 vs T2, FTS engine, SOAP, offline conflicts, retention clock) are created on the relevant Run's design task.
