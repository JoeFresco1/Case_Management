# Phase 1 Code Review — 001-case-management-platform

**Reviewed artifact:** `001-case-management-platform` first slice (working tree `main`, dirty)  
**Reviewer:** Kilo (read-only)  
**Date:** 2026-09-10  

---

## 1. Review scope

This review focuses on the Phase 1 preparation checklist requested by the caller:
- environment / lint / type / layer checks
- file-size audit
- test run + coverage
- architecture document existence

It also includes read-only findings from the first-slice backend and tests.  
No code was changed. The workspace is dirty; only the review document is added.

---

## 2. Environment checks

| Check | Command | Result |
|---|---|---|
| Ruff lint | `uv run ruff check .` | PASS — no issues in 153 source files |
| Ruff format | `uv run ruff format --check .` | PASS — 457 files already formatted |
| Mypy strict | `uv run mypy --strict app/domain app/application` | PASS — 153 files clean |
| Import layer | `uv run import-linter lint` | PASS — 3 contracts kept |
| Architecture gate | `python scripts/check_architecture_md.py` | PASS — `specs/001-case-management-platform/architecture.md` present with Mermaid fences |
| Docker / Postgres | `docker info` / `docker compose ps` | UNAVAILABLE on this host (expected per AGENTS.md §3). Sqlite fallback is used for tests. |

### Issue E-1 — tooling mismatch in agent contract
`AGENTS.md §4.R5` references `lint-imports verify`, but the installed CLI exposes `lint-imports` (no subcommand) and `import-linter lint`. The equivalent check passes, but the documented command will fail for agents copy-pasting the contract.

---

## 3. File-size audit

Files exceeding 1 000 lines (tracked source / tests / docs):

| File | Lines | Classification |
|---|---|---|
| `backend/app/domain/ports.py` | 2 464 | backend source — hard limit violation |
| `backend/app/infrastructure/persistence/models.py` | 1 611 | backend source — hard limit violation |
| `backend/app/application/workflow/execute_transition.py` | 1 209 | backend source — hard limit violation |
| `backend/app/domain/retention.py` | 1 106 | backend source — hard limit violation |
| `backend/app/application/webhooks/outbound.py` | 1 135 | backend source — hard limit violation |
| `backend/tests/retention/test_archival.py` | 1 193 | backend test |
| `backend/tests/webhooks/test_outbound.py` | 1 162 | backend test |
| `backend/tests/application/test_queue_admin.py` | 1 113 | backend test |
| `backend/tests/application/test_role_admin.py` | 1 022 | backend test |
| `frontend/src/api/types.gen.ts` | 14 126 | generated (not hand-authored) |
| `frontend/package-lock.json` | 5 220 | lockfile (not source) |
| `frontend/pnpm-lock.yaml` | 3 242 | lockfile (not source) |

`AGENTS.md §4.R2` sets a hard 1 000-line ceiling for source files and recommends refactoring past 600 lines. The five backend source files above breach the hard limit. Lockfiles and generated types are noted for completeness.

---

## 4. Test run

Command:
```
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q
```

Outcome:
- 1 157 passed, 0 failed
- coverage `app/domain` + `app/application`: **89.95 %** (≥ 80 % gate satisfied)
- duration: ~5m14s

### Re-run notes
A full-suite run earlier emitted one `ERROR` under `filterwarnings = ["error"]`:
`tests/interfaces/http/test_idempotency_atomic.py::test_same_key_same_request__executes_once_and_replays`
Failure mode: `PytestUnraisableExceptionWarning` from `aiosqlite.Connection.__del__` (`ResourceWarning: connection deleted before being closed`).  
Re-running that file in isolation (`7 passed`) shows the defect is **order-dependent**, not deterministic for that single test.

---

## 5. Architecture document

`specs/001-case-management-platform/architecture.md` exists and contains the required Mermaid fences (flowchart, ER, sequence diagrams). The CI gate script (`scripts/check_architecture_md.py`) passes.

---

## 6. Findings (read-only)

### High

**H-1 — Order-dependent teardown leak in idempotency HTTP tests**  
`backend/tests/interfaces/http/test_idempotency_atomic.py:58` creates an isolated `AsyncEngine`. The fixture calls `await engine.dispose()`, but a pooled `aiosqlite` connection survives into teardown under full-suite execution and is garbage-collected on a different event loop. The project enforces `filterwarnings = ["error"]`, so the promoted `ResourceWarning` aborts the suite. Impact: CI is flaky depending on test collection order. Evidence: full run failed at setup of the test; isolated run of that file passed.

**H-2 — Five backend source files exceed the 1 000-line hard limit**  
`backend/app/domain/ports.py`, `backend/app/infrastructure/persistence/models.py`, `backend/app/application/workflow/execute_transition.py`, `backend/app/domain/retention.py`, `backend/app/application/webhooks/outbound.py` are all >1 000 lines. `AGENTS.md §4.R2` forbids source files >1 000 lines.

### Medium

**M-1 — Global local-user registry never reset between tests**  
`backend/app/deps.py:87` stores seeded dev users in module-level `_LOCAL_USERS`. Tests register users in fixtures (`register_local_user(...)`), but no teardown removes them. The current suite does not collide, but future tests risk cross-contamination.

**M-2 — Heavy reliance on `suppress(Exception)` for tenant-listener best-effort**  
`backend/app/db.py:136` swallows *any* exception from `with_loader_criteria` injection. A future SQLAlchemy change or a malformed statement will silently skip the defense-in-depth filter. The intent is documented, but the blanket exception type hides real failures.

**M-3 — Brittle Starlette-deprecation suppression matrix**  
`backend/tests/conftest.py:99` and `backend/tests/integration/conftest.py:22` install multiple `DeprecationWarning` ignores to keep `filterwarnings = ["error"]` from breaking collection. The ignored `HTTP_422_UNPROCESSABLE_ENTITY` and `on_event` deprecations are pre-existing but actively monitored; the httpx/starlette.testclient ignore is a moving target. Any new deprecation in transitive dependencies will break collection again.

### Low / Risks

**L-1 — `load_case_type.py` is 0 % covered**  
`backend/app/application/configuration/load_case_type.py` has 18 statements, 0 % covered. Not a coverage gate failure overall (89.95 %), but a gap in the configuration read path.

**L-2 — `custom_reports.py` is below 80 %**  
`backend/app/application/reports/custom_reports.py` is 79 % covered. It is the only application module below the domain+application aggregate gate today; a small test addition would raise it.

**L-3 — Workspace is dirty and contains untracked first-slice artifacts**  
`git status` shows untracked frontend files (`frontend/src/cases/*.js`, `frontend/src/workflow/*`) and an `impeccable/` tree. None are referenced by the first-slice spec/plan. They are not part of the reviewed component and were not touched, but they indicate mixed worktree state.

**L-4 — `docker compose` not available in this environment**  
`docker compose ps` reports `docker: 'compose' is not a docker command.` The agent contract and README assume CLI-only Compose. Local Postgres cannot be started here; sqlite is the viable fallback.

---

## 7. Summary

- Lint / type / layer checks: **green**
- Test run: **green** under isolated file, but **order-dependent failure** under full suite (H-1)
- Coverage: **89.95 %** (gate met)
- Architecture doc: **present and valid**
- Source size: **5 hard-limit violations** (H-2)
- Docker / local Postgres: **not available** on this host (expected)

No implementation changes were made. Findings above are for triage only.
