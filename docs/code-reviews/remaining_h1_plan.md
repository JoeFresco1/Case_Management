# H1 Remainder — Build Plan (FM-017, FM-018, FM-026/027, FM-036, FM-039)

**Source:** `specs/pillars/01-deduplicated-feature-map.md` §4 pull order + `remaining_tasks.md`
**Status at handoff:** H1 FM-048/011/021/016/052/055 DONE, FM-031 DONE. All of H2 DONE. All of H3 DONE except 5 Gated switches (left off by design).
**Scope of this plan:** the six unbuilt H1 rows, in pull order. Nothing here re-opens DONE rows.

> How this was assessed (2026-09-11): stub scan over all routers (no 501s left
> except none — forms 501s were closed in H3), per-feature test suites green,
> route inventory at 123 OpenAPI paths, all dependency resolvers settle.
> Each slice below lists what EXISTS (do not rebuild) vs the genuine gap.

---

## 0. Conventions (carry into the new chat)

- Backend lives in `backend/` (`app/`, `tests/`, `alembic/`). PowerShell 5.1:
  no `head`/`tail`/`grep`/`&&`; use `;`. Test env per command:
  `$env:APP_ENV='test'; $env:DATABASE_URL='sqlite+aiosqlite:///./.pytest.db';`
- Test naming `test_<unit>_<scenario>__<expectation>`, given/when/then.
  Router tests: TestClient + `sql_session_factory` + production provider on
  `app.state` (pattern: `tests/interfaces/test_queues_router.py`).
- New persistence: SQLAlchemy generic types only (no JSONB/GIN/ILIKE), every
  query carries `WHERE tenant_id = :tenant_id`, compare-and-swap on
  `version_token` for mutations, new table → new Alembic migration (portable,
  reversible) + column assertions in
  `tests/foundation/test_baseline_migrations.py` + round-trip in
  `tests/foundation/test_migration_model_parity.py`.
- New HTTP surface: ports → provider bundle → router (never import
  infrastructure from interfaces), `authorize()` before DB touch,
  `await db.commit()` on mutations, structured errors (no stack/SQL leaks).
- Every slice ends with: `ruff check` + `ruff format --check` clean,
  `mypy --strict app/domain app/application` clean, `lint-imports` 3/3 kept,
  targeted `pytest` green, one ADR in `docs/decisions/ADR-NNN-*.md`
  (Context/Options/Decision/Consequences/Azure-seam), status flip in
  `remaining_tasks.md`.
- Known pre-existing drift (do NOT touch unless the slice requires it):
  `app/db.py` and `tests/application/configuration/test_load_case_type.py`
  fail `ruff format --check`; `models_forms.py` is dead code;
  `form_definitions/versions/bindings` tables have no migrations (tests run
  on `create_all`).

---

## Slice 1 — FM-017 Approval transitions & separation of duties (P02)

**Depends on:** FM-016 ✅ (queues operable; eligible approvers route through queues).
**Shippable outcome:** consequential transitions require an eligible second
actor; originator self-approval is refused with an explainable audit record.

### EXISTS — do not rebuild
- Domain: `WorkflowTransition.approval_required: bool`,
  `eligible_approver_roles: tuple[str, ...]` (`app/domain/workflow.py`);
  `WorkflowTransition(**json)` mapping passes them through
  (`app/infrastructure/persistence/mapping.py`).
- Engine: SoD gate in `app/application/workflow/execute_transition.py`
  (step 7 — allow / denied_originator / denied_role / bypass_system /
  not_required, stamped on the `WorkflowTransitioned` audit row).
- Tests: `tests/application/test_approval_sod.py` (5 tests, green).

### GAPS to build
1. **Authoring validation** — `app/application/configuration/validate_workflow_definition.py`
   never mentions approvals. Add rules: `approval_required` transitions must
   be `human_driven`; `eligible_approver_roles` entries must be non-empty
   (and, if a role registry read is cheap, must name real roles — otherwise
   document why stringly-checked). Tests in
   `tests/domain/test_workflow_validation.py` style (new file or extend).
2. **Preflight surfacing** — `app/application/workflow/preflight_transitions.py::handle`
   has no approval awareness. Add per-transition `approval_required`,
   `eligible` (would this actor satisfy SoD?), and `reason` so the UI can
   render "needs a second actor" *before* the worker attempts. Wire through
   the existing preflight endpoint (check `tasks.py`/cases router for the
   caller). Tests: originator sees `eligible=false`, second actor `true`,
   ineligible role `false` with reason.
3. **Pending-approval visibility (iff cheap)** — if (1)+(2) satisfy the
   outcome, skip. Only add a "transitions awaiting approval" query if the
   pilot needs it; do NOT invent an approval-request ticket object — the
   engine model is single-step (the *other* actor executes), and a ticket
   system would be a new feature, not FM-017.

### Acceptance
- `uv run pytest tests/application/test_approval_sod.py tests/domain/test_workflow_validation.py tests/interfaces -q -p no:cacheprovider --no-cov` green.
- Manual: publish workflow with `approval_required` transition → originator
  `POST transition` gets 403 with SoD reason → second actor succeeds →
  timeline row carries `sod_outcome: allowed`.
- Flip `remaining_tasks.md` FM-017 → DONE.

---

## Slice 2 — FM-018 SLA deadlines & escalation (P02)

**Depends on:** FM-014 ✅ (basic seam: `start_sla` actions, scanner, `sla_state` in summaries).
**Shippable outcome:** workers see deadline state; a breach deterministically
fires configured escalation actions visible in the case timeline.

### EXISTS — do not rebuild
- Domain: `app/domain/sla.py` (clocks, tiers); scanner
  `app/application/sla/scan_sla.py` (`scan_once`, `scan_warning_once`,
  `_dispatch_escalation` with `create_task` support); scheduler wired in
  `app/main.py` (`sla_scanner`); `SlaStateView` in case summaries;
  `SqlSlaScanner` + `SqlSlaClockRepository`.
- Tests: SLA suites green (covered in earlier runs).

### GAPS to build
1. **Authoring path** — verify/close the loop: workflow `start_sla` action
   params (clock key, deadline, escalation action) validated at publish
   time in `validate_workflow_definition.py`; escalation `create_task`
   requires a queue — validator should reject queue-less escalation
   (runtime already fails closed). Add validator rules + tests.
2. **Breach visibility** — confirm the escalation audit/timeline entry
   renders the fired action (task created + queue) and the no-op case
   (`notify` placeholder reports "no action fired"). Add a scanner-level
   test: breach clock → task row exists → timeline entry references it.
3. **`notify` decision** — explicitly out of scope (would be email/IM
   fan-out; no transport exists). Document as deferred in the ADR, keep
   the honest `escalation_fired=false` audit signal.

### Acceptance
- `uv run pytest tests -q -p no:cacheprovider --no-cov -k "sla or Sla or SLA"` green
  (plus full `tests/application` + `tests/interfaces` green).
- Manual: case with breached clock → scanner tick → escalation task in
  queue → timeline shows breach + action.
- Flip `remaining_tasks.md` FM-018 → DONE (it is currently unlisted as
  PARTIAL — add the row).

---

## Slice 3 — FM-026 Import mapping, dry run, reconciliation (P14) + FM-027 Ingestion-channel governance & quarantine (P04)

**Do these together** (FM-027 is BLOCKED on FM-026; same code area).
**Shippable outcome:** operators import CSV/JSON/JSONL through validated
mappings, preview effects, commit accepted records, reconcile results;
malformed items isolate per-record with inspect/release/reject.

### EXISTS — do not rebuild
- Use cases: `preview` + `commit` in `app/application/imports/run_import.py`
  (row evaluation, document policy, `_reconciliation_from`, per-row error
  codes); endpoints `POST /imports/dry-run` + `POST /imports/commit`
  (`app/interfaces/http/routers/imports.py`); `MappingVersion`
  (validated, pinned on jobs); quarantine repo + channel endpoints
  (`app/interfaces/http/routers/channels.py`, 7 routes); tests green
  (`tests/application/test_import.py`, `test_quarantine.py`, `test_capture/*`).

### GAPS to build
1. **Mapping registry** — mappings are request-supplied today; FM-026 says
   "validated *versioned* mappings". Add: `import_mappings` table
   (key, version_number, mapping_json, status draft/published/retired) +
   migration + SQL repo + admin CRUD (`GET/POST /admin/import-mappings`,
   publish/retire) + `preview`/`commit` accept `mapping_id` as an
   alternative to the inline mapping (inline stays for back-compat).
   Tests: version pinning (job records version; re-run of old version
   reproduces), retire blocks new jobs, tenant isolation.
2. **Reconciliation surfacing** — `_reconciliation_from` exists; verify the
   commit response carries accepted/rejected counts + per-row codes and
   add a `GET /imports/{job_id}` (or equivalent) if the operator cannot
   retrieve a past reconciliation today. Check first — build only the gap.
3. **Quarantine closure** — verify per-record inspect/release/reject over
   HTTP against the SQL quarantine repo (not fakes); add router tests if
   missing. Malformed-item isolation must be per-record (one bad row never
   fails the batch) — pin with a test if not already pinned.

### Acceptance
- `uv run pytest tests/application/test_import.py tests/application/test_quarantine.py tests/capture tests/interfaces -q -p no:cacheprovider --no-cov` green.
- Manual: publish mapping v1 → dry-run CSV → commit → reconciliation counts
  match → malformed row quarantined → inspect → release/reject.
- Flip `remaining_tasks.md` FM-026 + FM-027 → DONE.

---

## Slice 4 — FM-036 Versioned conditional case forms (P07)

**Depends on:** FM-021 ✅ (governance), FM-038 ✅ (sessions/saving done in H3).
**Shippable outcome:** intake submits a published case-bound form; sections,
conditions, and case-type bindings versioned; hidden-field behavior safe.

### EXISTS — do not rebuild
- `submit_form.handle` (`app/application/forms/submit_form.py`) with
  `visible_when` conditions, hidden-field preservation, per-step/full
  required validation; bindings (`draft`→published) + versions; transition
  form check (`check_transition_forms`); multi-step sessions (H3);
  `tests/forms/*` + `tests/application/forms/*` green.

### GAPS to build (verify-first; this slice may be small)
1. **Binding lifecycle audit** — verify draft→publish→retire exists for
   *bindings* (versions have it; bindings have `published_at` — check for
   retire). Add retire iff missing, else document.
2. **Case-bound submit path** — verify an intake client can discover the
   right form for a case type (binding lookup by case-type version over
   HTTP) and submit it end-to-end; add the lookup endpoint or test only
   for what's missing.
3. **Condition semantics pin** — hidden-field preservation + required-only-
   when-visible already tested; add one router-level test proving a
   conditionally-hidden required field does NOT block submit.

### Acceptance
- `uv run pytest tests/forms tests/application/forms tests/interfaces -q -p no:cacheprovider --no-cov` green.
- Manual: publish binding → submit with hidden required field → 200, stored
  case validates.
- Flip `remaining_tasks.md` FM-036 → DONE.

---

## Slice 5 — FM-039 Legal-hold lifecycle, full (P08)

**Depends on:** FM-003 ✅. (FM-040 already DONE against the refusal seam.)
**Shippable outcome:** security admins place and release audited holds that
block disposition of the case, documents, and audit evidence.

### EXISTS — do not rebuild
- Endpoints `POST` + `DELETE /cases/{id}/holds`
  (`app/interfaces/http/routers/holds.py`); use cases `place_hold` /
  `release_hold` (`app/application/holds/manage_holds.py`); `Case.legal_hold`
  refusal on close; tests green.

### GAPS to build
1. **SQL durability** — replace `InMemoryLegalHoldRepository`
   (`app.state.legal_hold_repository` in `app/main.py`) with a SQL adapter:
   `legal_holds` table (case_id, tenant_id, placed_by/at, reason,
   released_by/at, status) + migration + repo + provider swap. In-memory
   loses holds on restart = fail-open disposition risk. Tests: place →
   restart-equivalent (new session) → hold still blocks close; release →
   close succeeds; cross-tenant isolation; idempotent double-place.
2. **Disposition interplay** — verify the FM-040 disposition flow consults
   holds (eligible/routed cases under hold refuse with an explainable
   reason); wire it if it only checks `Case.legal_hold`.
3. **Audit prominence** — place/release rows already audited; confirm the
   case timeline surfaces hold state (add a `legal_hold` flag to the case
   summary if absent — check `build_summary` first).

### Acceptance
- `uv run pytest tests/application/test_legal_hold.py tests/retention tests/interfaces -q -p no:cacheprovider --no-cov` green.
- Manual: place hold → close/dispose refused → release → close succeeds →
   timeline shows both.
- Flip `remaining_tasks.md` FM-039 → DONE (currently PARTIAL).

---

## Run order & parallelism

```
Slice 1 (FM-017) ── can start immediately ─┐
                                           ├─► Slice 5 (FM-039) last
Slice 3 (FM-026/027) ── can run PARALLEL ──┤  (touches disposition interplay)
Slice 4 (FM-036) ── can run PARALLEL ──────┘
Slice 2 (FM-018) ── anytime (touches validator shared with Slice 1 —
                     coordinate or sequence after Slice 1)
```

- `maxConcurrency=4` per AGENTS.md, but keep it to ≤2 slices concurrently;
  Slices 1+2 both touch `validate_workflow_definition.py` — sequence them.
- One Orca Run per slice (`orca orchestration run-create --objective "FM-0XX: ..."`),
  4–12 tasks each, gates at design-approval and pre-publish.

## Global definition of done (each slice)

1. All slice tests + affected suites green (commands in each slice).
2. `ruff check` + `ruff format --check` clean on touched files.
3. `mypy --strict app/domain app/application` clean; `lint-imports` 3/3 kept.
4. New tables: migration renders valid portable SQL
   (`alembic upgrade <prev>:head --sql`), columns asserted in
   `test_baseline_migrations.py`, round-trip in
   `test_migration_model_parity.py` (downgrade budget auto-counts — no change needed).
5. ADR in `docs/decisions/ADR-NNN-*.md` + `remaining_tasks.md` flip.
6. No Gated switches flipped (SIEM stream, session policy/cert, named
   connectors, physical scanner stay off — unanimously out of scope).

## Explicit non-goals

- Frontend builds for the new surfaces (backend OpenAPI-first; UI follows).
- `notify` escalation fan-out, approval ticket objects, real-time sync,
  physical scan device — deferred with reasons in each slice ADR.
- Re-opening any DONE row (H1/H2/H3) except the two noted touch-ups
  (validator additions are additive-only; `SaveFormStepCommand` stays
  backward-compatible).
