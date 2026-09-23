# T0-5 Ordered Commit Plan — Uncommitted H1/H2/H3 Work

**Inputs read:** `remaining_tasks.md` (all H1/H2/H3 rows DONE per 2026-09-11), `remaining_h1_plan.md` (5-slice build plan + conventions), `git status/diff --stat` (73 modified, ~60 untracked), gate samples (ruff / format / mypy / lint-imports), ports-package + migration inventory.
**Outputs:** no source changes made; ordered plan below.
**Acceptance:** 8 slice commits proposed, each with exact files, owning FM, gate status; revert list flagged. Nothing committed.

## Baseline gate snapshot (T0-1, sampled — full pytest timed out, NOT green-claimed)

- `ruff check app`: 1 error — `PLR0912` too many branches in `app/application/workflow/start_workflow.py:148` (`handle`).
- `ruff format --check app`: 11 files unformatted (see Commit 0 — must be fixed pre-commit).
- `mypy --strict app/domain app/application`: 3 errors — `validate_workflow_definition.py:688` (`float(Any|None)`), `start_workflow.py:257,258` (unused `type: ignore`).
- `lint-imports`: 3/3 KEPT (Domain pure, Application→domain only, Interfaces↛infrastructure).
- `pytest tests/foundation/test_baseline_migrations.py + test_migration_model_parity.py`: TIMED OUT at 120s in sqlite profile — migration parity is the highest-risk unverified gate; must pass per-slice before commit (see per-commit gates).

## Ports review (T0-2)

- `backend/app/domain/ports.py` (2464 lines) deleted → `backend/app/domain/ports/` package (20 modules + `__init__.py` re-exporting all 53 old names). All 89 `from app.domain.ports import …` sites still resolve; lint-imports 3/3 kept. Verdict: COMMITTABLE as part of Commit 1 stack, keep as pure move (no behavior change inside the split).
- Anomaly: `ReferenceFormatRepository` / `InMemoryReferenceFormatRepository` live in `app/domain/reference_format.py`, and form/import-mapping repos live beside their aggregates — not re-exported from `ports/__init__.py`. Acceptable (import-linter clean) but each owning commit should either re-export or document the exception. Do NOT "fix" by moving code in this plan.

## Migration audit (T0-3)

- 8 new untracked migrations: `0020_queue_version_token`, `0021_reference_formats`, `0022_widen_case_reference`, `0023_break_glass_grants`, `0024_form_sessions`, `0025_import_mappings`, `0026_create_legal_holds_table`, `0027_import_mapping_and_form_binding_lifecycle`. Each must ship in its owning slice commit together with its model + repository + column assertions in `test_baseline_migrations.py` + round-trip in `test_migration_model_parity.py` (currently updated in working copy — see Commit 5). `alembic upgrade <prev>:head --sql` must render portable SQL per slice.
- DUPLICATE (must resolve before commit): `backend/app/infrastructure/persistence/legal_hold_repository.py` (tracked, modified) vs `backend/app/infrastructure/persistence/repositories/legal_hold_repository.py` (untracked). Only one canonical SQL adapter may be committed — delete the shadow and fix imports in the owning commit (Commit 4). By contrast `SqlQueueRepository` moving out of `workflow_repository.py` into `repositories/queue_repository.py` is a clean move — keep.

## FM inventory (T0-4)

Working-copy changes map to DONE rows only (no new-FM scope creep): FM-035, FM-031/032/033/034, FM-036/038, FM-026/027, FM-016/017/018, FM-009, FM-010, FM-055/056/057. `remaining_tasks.md`/`remaining_h1_plan.md` themselves are untracked planning docs — commit once in Commit 0, not per slice.

## Commit 0 — Hygiene + planning docs (no FM; prerequisite to all)

Files: `remaining_tasks.md`, `remaining_h1_plan.md`, `docs/decisions/ADR-030-*.md`, `ADR-031-*.md`, `ADR-032-*.md`, `ADR-033-*.md`, `ADR-034-*.md`, `ADR-035-*.md`, `specs/002-leftovers/{spec,plan}.md` (if retained).
Gates: `ruff format` on the 11 files below + fix the 3 mypy errors + `PLR0912` (extract helper or add justified `noqa`) BEFORE any slice commit; re-run `ruff check`, `ruff format --check`, `mypy --strict`, `lint-imports`.
Format-touch list: `app/application/cases/view_case.py`, `app/application/imports/admin/__init__.py`, `app/application/imports/http_dependencies.py`, `app/application/workflow/start_workflow.py`, `app/db.py`, `app/domain/form.py`, `app/infrastructure/persistence/imports_http_dependencies.py`, `app/infrastructure/persistence/repositories/import_mapping_repository.py`, `app/infrastructure/persistence/repositories/legal_hold_repository.py`, `app/interfaces/http/routers/admin_form_bindings.py`, `app/interfaces/http/routers/imports.py`.
REVERT (do not commit): `.orca-dag.config.json` (runId/harness churn is run-state).

## Commit 1 — Security break-glass (FM-035; first — enforcement touches search/queues consumers)

Modified: `backend/app/application/security/break_glass.py`, `backend/app/application/security/http_dependencies.py`, `backend/app/infrastructure/persistence/security_http_dependencies.py`, `backend/app/interfaces/http/routers/security.py`, `backend/app/interfaces/http/schemas/cases.py` (check hunk — revert if unrelated), `backend/tests/security/test_break_glass.py`, `backend/tests/security/test_tenant_isolation.py`.
New: `backend/alembic/versions/0023_break_glass_grants.py`, `backend/app/infrastructure/persistence/repositories/break_glass_repository.py`, `backend/tests/security/test_break_glass_enforcement.py`, `docs/decisions/ADR-031-break-glass-enforcement.md`, `backend/app/domain/ports/` package split (pure move; `ports.py` deletion ships here).
Gates: mypy clean (touches `break_glass.py`); `pytest tests/security -q` green; tenant-isolation test with 2 tenants; portable-SQL check on 0023.

## Commit 2 — Search stack (FM-031 + FM-032 + FM-033 + FM-034)

Modified: `backend/app/application/search/http_dependencies.py`, `backend/app/infrastructure/persistence/search_http_dependencies.py`, `backend/app/interfaces/http/routers/search.py`, `backend/tests/search/test_document_search.py`, `frontend/src/search/SearchPage.tsx`, `frontend/src/search/SearchPage.module.css`, `frontend/src/api/client.ts` (check hunk — revert if unrelated), `backend/tests/events/test_event_catalog.py` (check hunk — move to Commit 6 if event-catalog only).
New: `frontend/src/search/SearchAudit.tsx`, `frontend/src/search/SearchAudit.module.css`, `backend/tests/interfaces/test_semantic_search_router.py`.
Gates: `pytest tests/search tests/interfaces -q` green; redaction-snippet assertions (FM-011 interplay); `pnpm typecheck && pnpm build`.

## Commit 3 — Forms (FM-036 + FM-038)

Modified: `backend/app/application/forms/advanced_execution.py`, `backend/app/application/forms/http_dependencies.py`, `backend/app/domain/form.py`, `backend/app/infrastructure/persistence/forms_http_dependencies.py`, `backend/app/infrastructure/persistence/mapping.py` (form hunks only — split queue/case hunks to their commits), `backend/app/infrastructure/persistence/repositories/form_repository.py`, `backend/app/interfaces/http/routers/forms.py`, `backend/tests/application/forms/test_form_submission.py`, `backend/tests/foundation/test_migration_model_parity.py` (form hunks only).
New: `backend/alembic/versions/0024_form_sessions.py`, `backend/alembic/versions/0027_import_mapping_and_form_binding_lifecycle.py` (form-binding half; co-ships with Commit 4 — if alembic ordering forbids splitting, move whole file to Commit 4 and note it), `backend/app/application/forms/admin/`, `backend/app/infrastructure/persistence/admin_form_bindings_http_dependencies.py`, `backend/app/interfaces/http/routers/admin_form_bindings.py`, `backend/app/interfaces/http/schemas/admin_form_bindings.py`, `backend/tests/forms/test_form_session_sql.py`, `backend/tests/interfaces/test_form_sessions_router.py`, `backend/tests/application/configuration/` (approval/sla validation tests live here — move mis-filed ones to Commit 5).
Gates: `pytest tests/forms tests/application/forms tests/interfaces -q` green; hidden-required-field router test; portable-SQL on 0024.

## Commit 4 — Imports (FM-026 + FM-027)

Modified: `backend/app/application/imports/run_import.py`, `backend/app/application/imports/http_dependencies.py`, `backend/app/infrastructure/persistence/imports_http_dependencies.py`, `backend/app/infrastructure/persistence/models.py` (import hunks only), `backend/app/interfaces/http/routers/imports.py`, `backend/app/interfaces/http/schemas/imports.py`, `backend/tests/foundation/test_baseline_migrations.py` (import hunks only).
New: `backend/alembic/versions/0025_import_mappings.py`, `backend/app/domain/import_mapping.py`, `backend/app/application/imports/admin/`, `backend/app/infrastructure/persistence/repositories/import_mapping_repository.py`, `backend/app/infrastructure/persistence/admin_import_mappings_http_dependencies.py`, `backend/app/interfaces/http/routers/admin_import_mappings.py`, `backend/app/interfaces/http/schemas/admin_import_mappings.py`.
Gates: `pytest tests/application/test_import.py tests/application/test_quarantine.py tests/capture tests/interfaces -q` green; per-record quarantine pin; portable-SQL on 0025.
Also resolves the 0027 co-ownership with Commit 3 (see above).

## Commit 5 — Queues + workflow approvals/SLA + references (FM-016 + FM-017 + FM-018 + FM-009)

Modified: `backend/app/application/queues/manage_queues.py`, `backend/app/application/configuration/validate_workflow_definition.py`, `backend/app/application/workflow/execute_transition.py`, `backend/app/application/workflow/preflight_transitions.py`, `backend/app/application/workflow/start_workflow.py` (also owns the PLR0912 + unused-ignore fixes from Commit 0), `backend/app/infrastructure/persistence/mapping.py` (workflow/queue hunks), `backend/app/infrastructure/persistence/sla_scanner.py`, `backend/app/infrastructure/persistence/admin_references_http_dependencies.py`, `backend/app/infrastructure/persistence/repositories/workflow_repository.py` (SqlQueueRepository removal), `backend/app/infrastructure/persistence/repositories/case_repository.py` (reference-issuance hunks), `backend/app/interfaces/http/routers/queues.py`, `backend/app/interfaces/http/schemas/queues.py`, `backend/app/domain/events.py` (queue/task events — check hunk, move to Commit 6 if catalog-only), `backend/tests/reports/test_custom_reports.py` (+520 lines — verify scope; split non-queue parts out), `backend/tests/reports/test_dashboard.py`, `backend/tests/reports/test_operational_reports.py`.
New: `backend/alembic/versions/0020_queue_version_token.py`, `backend/alembic/versions/0021_reference_formats.py`, `backend/alembic/versions/0022_widen_case_reference.py`, `backend/app/application/queues/http_dependencies.py`, `backend/app/infrastructure/persistence/queue_http_dependencies.py`, `backend/app/infrastructure/persistence/repositories/queue_repository.py`, `backend/app/infrastructure/persistence/repositories/reference_format_repository.py`, `backend/tests/interfaces/test_queues_router.py`, `backend/tests/application/test_preflight_approval.py`, `backend/tests/application/test_sla_breach_visibility.py`, `backend/tests/domain/test_workflow_approval_validation.py`, `backend/tests/domain/test_workflow_sla_validation.py`, `backend/tests/application/references/`, `docs/decisions/ADR-030-*.md`, `ADR-032-*.md`, `ADR-034-*.md`, `ADR-035-*.md`.
Gates: `pytest -k "sla or approval or queue or reference"` + full `tests/application tests/interfaces` green; portable-SQL on 0020/0021/0022.

## Commit 6 — Webhooks + events + GraphQL (FM-055 + FM-056 + FM-057)

Modified: `backend/app/application/webhooks/outbound.py`, `backend/app/domain/events.py` (catalog hunks), `backend/app/main.py` (dispatch-loop wiring — split legal-hold/provider hunks to Commit 7), `backend/app/config.py`, `backend/app/deps.py`, `backend/app/interfaces/http/dependencies.py`, `backend/tests/events/test_event_catalog.py` (catalog hunks).
New: `backend/app/infrastructure/webhooks/delivery_dispatcher.py`, `backend/app/interfaces/http/routers/graphql.py`, `backend/tests/webhooks/test_delivery_dispatcher.py`, `backend/tests/interfaces/test_graphql_router.py`, `docs/decisions/ADR-033-h3-slices-and-gates.md`.
Gates: `pytest tests/events tests/webhooks tests/interfaces -q` green; signed-delivery + replay assertions; no Gated switches flipped (SIEM stream stays off).

## Commit 7 — Relationships, legal holds, cases core (FM-010 + FM-039 + FM-005/008 carry)

Modified: `backend/app/application/cases/create_case.py`, `backend/app/application/cases/split_case.py`, `backend/app/application/cases/summary.py`, `backend/app/application/cases/view_case.py`, `backend/app/application/cases/relationship_http_dependencies.py`, `backend/app/domain/case.py`, `backend/app/domain/form.py` (if case-bound hunks remain), `backend/app/infrastructure/persistence/case_relationship_http_dependencies.py`, `backend/app/infrastructure/persistence/legal_hold_repository.py` OR its `repositories/` twin (delete the shadow — see Migration audit), `backend/app/infrastructure/persistence/repositories/case_repository.py` (relationship hunks), `backend/app/infrastructure/persistence/repositories/form_repository.py` (binding-lifecycle hunks), `backend/app/interfaces/http/routers/cases.py`, `backend/app/interfaces/http/routers/case_relations.py`, `backend/app/interfaces/http/schemas/cases.py` (case hunks), `backend/app/cli/seed.py`, `backend/app/db.py`, `backend/app/deps.py` (hold-provider swap), `backend/app/main.py` (hold-provider swap), `backend/tests/application/cases/_fakes.py`, `backend/tests/application/cases/test_view_case.py`, `backend/tests/conftest.py`, `backend/tests/integration/conftest.py`, `backend/tests/interfaces/http/test_idempotency_atomic.py`.
New: `backend/alembic/versions/0026_create_legal_holds_table.py`, `backend/app/infrastructure/persistence/repositories/legal_hold_repository.py` (canonical — keep exactly one), `backend/tests/application/configuration/` (any case-config tests).
Gates: `pytest tests/application/cases tests/interfaces tests/integration -q` green; hold-blocks-close + release-allows-close + cross-tenant isolation; portable-SQL on 0026.

## Commit 8 — Frontend workspace (generated .tsx only)

Modified: `frontend/src/App.tsx`, `frontend/src/main.tsx`, `frontend/src/api/client.ts` (if still unassigned after Commits 1–2 triage), `frontend/src/search/*` already counted in Commit 2 — do not double-count.
REVERT (do not commit): all `frontend/src/**/*.js` twins (`cases/CaseList.js`, `CreateCasePage.js`, `workflow/WorkflowAdmin.js`, `api.js`, `index.js`, …) — stale transpile artifacts beside canonical `.tsx`; committing them forks the frontend. Also revert `frontend/src/cases/CreateCasePage.test.js` (stale twin of `.tsx` test).
Gates: `pnpm typecheck && pnpm build` green; no hand-typed API shapes (regenerate via `openapi-typescript`).

## REVERT list (must NOT be committed anywhere)

- `.orca-dag.config.json` (run-state churn), `.codegraph/`, `.codex-remote-attachments/`, `impeccable/` (tool/vendor dumps), `Phase2_codereview_*.md`, `phase3_codereview_architecture.md`, `phase_1_code_review.md` (review scratch — file as PR comments or move to `docs/decisions/` deliberately, never bulk-commit), `docs/ORCHESTRATION-ROADMAP.md` + `docs/PRD-remaining-pillars.md` (unreviewed planning drafts — separate review before committing), all `frontend/src/**/*.js` twins listed above.

## Suggested commit order

Commit 0 (hygiene) → 1 (break-glass) → 5 (queues/approvals/SLA/refs — unblocks validators shared with 1) → 2 (search) → 3 (forms) → 4 (imports) → 7 (relationships/holds/cases) → 6 (webhooks/events/GraphQL) → 8 (frontend). Commits 2/3/4 are parallelizable after 1+5 (disjoint files except `models.py`/`mapping.py`/`test_migration_model_parity.py` — split by hunk, never whole-file).
