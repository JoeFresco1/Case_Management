# Phase 3 Code Review — Architecture & Decisions

**Scope:** `specs/001-case-management-platform/architecture.md` (Mermaid + module map + ER +
sequences + seams table), `docs/integrations/seams.md` (8-seam inventory),
`docs/decisions/ADR-*.md` (29 files), `scripts/check_architecture_md.py` (R9 CI gate),
and their fidelity to the implementation under `backend/app/`.
**Date:** 2026-09-10. **Mode:** review only — no code changed.
**Method:** read each doc end-to-end; cross-checked every structural claim against the
code (ports, routers, models, middleware, placeholders, health); ran the R9 gate script;
scanned all 29 ADRs for the Context/Options/Decision/Consequences/Seam template.

## Verdict

The Phase 3 artifacts **exist and are substantially accurate**. The R9 gate passes, all 6
Mermaid diagrams are well-formed, the 8-seam inventory matches the placeholder
directories on disk, and the core invariants the docs promise (same-transaction
audit+outbox, portable types, fail-closed identity, Page 50/200, hash-chained audit)
were all confirmed in code. **No load-bearing bug** (wrong transaction boundary,
missing tenant filter, leaked secret, broken gate) was found in the reviewed surface.

However the review produced **15 issues**: 2 medium, 13 low. The dominant theme is
**drift between docs and a codebase that has grown past the first slice**:
stale `file:line` references, a module map that describes the early slice layout, a
three-way adapter-label disagreement between `architecture.md` §7.1 / `health.py` /
`main.py` startup log, an ADR-020 index that omits 3 ADRs and mis-describes the
numbering gap, and one audit-hash formula omission that would break any independent
replay implementation written from the doc alone. Details below; nothing here blocks
the slice, but issues M-1, M-2 and L-1 should be fixed before the next slice lands.

---

## 1. What was checked and confirmed working

| Claim in docs | Verification | Result |
|---|---|---|
| `architecture.md` exists with Mermaid fences (R9) | `uv run python scripts/check_architecture_md.py` → gate OK, 1 slice; 6 ` ```mermaid ` fences found (flowchart LR, erDiagram, sequence, flowchart TD, flowchart LR, sequence) | PASS |
| Audit + outbox commit atomically with domain change (§5, §9) | `backend/app/application/cases/create_case.py:130-140,212-260` stages case + `audit.append` + `outbox.enqueue` then `start_workflow.handle` in the caller's transaction; `get_db` (`backend/app/deps.py:124-137`) shares the idempotency transaction | PASS (with nuance, see L-8) |
| Audit hash chain `SHA-256(prev + canonical(payload))` | `backend/app/domain/audit.py:66-75` implements chain + `verify` replay at `:313-377` | PASS in substance, formula imprecise — see **M-1** |
| Portable types only, no JSONB/GIN/ILIKE | `models.py` uses `SAUuid()`, `JSON`, `DateTime(timezone=True)`; `Tenant` block `models.py:37-43` confirms | PASS |
| Fail-closed dev identity (`deps.py:98`, 401 outside `local`) | `backend/app/deps.py:94-121` — `get_current_user` raises 401 unless `settings.is_local()` | PASS (line ref accurate; header nuance — see L-7) |
| Optimistic concurrency `If-Match: <version_token>` → 409 | `routers/cases.py:418-469`, ETag set at `:518-520` | PASS |
| Pagination `Page[T]` default 50 / max 200 | `backend/app/interfaces/http/pagination.py:27-49` (`DEFAULT_LIMIT=50`, `MAX_LIMIT=200`) | PASS |
| Tenant isolation: explicit `tenant_id` + listener is defense-in-depth | `domain/ports.py:7-29` contract doc; `db.py:71-142` opt-in listener; every port method takes `tenant_id` first | PASS |
| 8 placeholder dirs with `INTEGRATION_SEAM` markers + READMEs, no Azure SDK imports | All 8 `*_placeholder/README.md` present (`persistence/azure_sql_placeholder`, `storage/azure_blob_placeholder`, `messaging/azure_servicebus_placeholder`, `identity/entra_placeholder`, `secrets/azure_keyvault_placeholder`, `telemetry/azure_monitor_placeholder`, `ai/azure_openai_placeholder`, `imports/onbase_panzura_placeholder`) | PASS |
| Roadmap §7 ↔ ADR cross-links (R8) | `specs/pillars/00-product-roadmap.md:129-137` links ADR-021/002/028/012/022 and names ADR-020 as index; ADR-020 §"Roadmap §7 cross-link" mirrors it | PASS |
| Synthetic marker in logs + UI | `main.py:445-449` logs `SYNTHETIC ENVIRONMENT` at WARNING; `frontend/src/shell/Banner.tsx:27-40` reads `GET /api/v1/health → synthetic_environment` | PASS |
| Frontend types generated, not hand-typed | `frontend/src/api/types.gen.ts` exists alongside `client.ts` | PASS |

---

## 2. Issues

### M-1 (medium) — Audit hash formula in docs omits the `0x00` domain separator

- **Docs:** `architecture.md` §5 (`hash = SHA-256(prev_hash + canonical(payload))`) and §8
  (`hash = SHA-256(prev_hash + canonical(payload))`) both give the two-part concatenation.
- **Code:** `backend/app/domain/audit.py:24` and `:66-75` define
  `hash = SHA-256(prev_hash + 0x00 + canonical(payload_json))` — a literal `0x00` byte is
  hashed between the two parts (`h.update(prev_hash…); h.update(b"\x00"); h.update(canonical…)`).
- **Impact:** any independent verifier, export consumer, or future Azure adapter
  implemented from the architecture doc alone will compute different hashes and conclude
  the chain is broken. The in-repo `verify` path (`audit.py:313-377`) is unaffected
  because it calls `compute_hash`, but the doc is the contract for external readers.
- **Fix:** add `+ 0x00 +` to both formula sites and state that the separator is a single
  zero byte, not the two characters `0x00`.

### M-2 (medium) — ADR-020 index is missing ADRs and mis-describes the numbering gap

`docs/decisions/ADR-020-final.md` is the self-declared "single source of truth" for
"where do I find the decision that backs X", but:

1. The decision-index table (§"Decision index — first slice (ADR-001 .. ADR-028)") omits
   **ADR-003-sla-seam, ADR-018-migrations, and ADR-029-welcome-transition-task-dedup**
   entirely. ADR-029 postdates ADR-020 (so its absence is at least stale, not wrong), but
   ADR-003 and ADR-018 predate it and are material (SLA seam; migrations).
2. The numbering-gap note ("003 was reserved for an early multi-tenant discussion later
   renumbered as ADR-021; 018 was reserved for telemetry schema and absorbed into
   ADR-002") is **factually wrong**: `ADR-003-sla-seam.md` and `ADR-018-migrations.md`
   both exist on disk with real content.
3. The CI-gate section claims the check script is "stdlib-only and <50 lines" — the
   script is stdlib-only (true) but is **92 lines** (`scripts/check_architecture_md.py`).
4. The index header says "ADR-001 .. ADR-028" while the table skips ADR-003 and ADR-018
   without mention, so a reader cannot tell omission-by-design from omission-by-accident.
- **Fix:** add the three missing rows (or record why a row is deliberately absent),
  correct or delete the numbering-gap note, and fix "<50 lines" → actual count.

### L-1 (low) — Three-way adapter-label drift: architecture §7.1 vs `health.py` vs `main.py` startup log

The same seam inventory is rendered three ways with different keys and labels:

- `architecture.md` §7.1 shows **8 keys** (`db: local-postgres|azure-sql`, `store`,
  `bus`, `idp`, `secrets`, `telemetry`, `ai`, `imports`).
- `GET /api/v1/health` (`backend/app/interfaces/http/routers/health.py:108-130`) returns
  **10 keys** — adds `siem_stream` and `signed_batch_store` — and `_db_label` (`:52-71`)
  can also return `local-sqlite`, a value §7.1 never mentions.
- The startup log (`backend/app/main.py:423-439`) uses yet another vocabulary:
  `db: "local-sqlalchemy"`, `store: "local"`, `bus: "local"`, `ai_advisor: "local-noop"`,
  `enterprise_import: "local-synthetic-fixtures"` — none of which string-match the
  `local-postgres / local-fs / local-outbox / noop` labels in §7.1 or health.
- An operator diffing the startup log against `/health` against the doc sees three
  different answers for "which adapter is active". The seam *set* is consistent; only
  the labels disagree.
- **Fix:** pick one label vocabulary (recommend the health endpoint's, since it is the
  operator-visible contract per FR-034), copy it into §7.1 and the startup log, and add
  the two SIEM/signed-batch keys to §7.1 (they are real seams with real code:
  `infrastructure/persistence/siem.py`, `InMemorySignedBatchRepository`).

### L-2 (low) — `models.py` is 1611 lines, breaching the R2 hard limit the doc itself cites

- `backend/app/infrastructure/persistence/models.py` is **1611 lines** (measured).
  AGENTS.md §4.R2 (cited in `architecture.md` §12 gate: "no file >1000 lines") sets a
  hard 1000-line limit. The codebase already began the split (`models_forms.py` holds
  `FormDefinition/FormVersionORM/FormBindingORM`), but the main file kept growing
  (36 `__tablename__` entries: tenants → disposition_requests, including P04/P06/P07/P08
  tables the ER sketch does not cover).
- **Fix:** continue the sharding already started (e.g. `models_audit.py`, `models_docs.py`,
  `models_workflow.py`) or record an ADR exception. At minimum, `architecture.md` §4
  should acknowledge the split instead of implying one `models.py`.

### L-3 (low) — Stale `file:line` references throughout `architecture.md`

Spot-checked references against current sources (`ports.py` 2464 lines, `main.py` 501,
`deps.py` 191, `config.py` 144, `db.py` 218):

- `app/main.py:56` ("mounts health + cases routers") — line 56 is an import; the factory
  is `create_app` at `main.py:245`, routers mount at `:317-376` (now ~30 routers, not two).
- `app/main.py:39`, `app/main.py:95`, `app/main.py:25` — correlation middleware is at
  `:167-202`, synthetic log at `:445-449`, telemetry wiring at `:247`. All stale.
- `app/config.py:34` (`config.documents_path`) — `documents_path` is at `config.py:66`.
- `app/db.py:39` (`# INTEGRATION_SEAM: azure-sql` marker) — **no `INTEGRATION_SEAM`
  marker exists anywhere in `db.py`**; the seam comment lives in `deps.py:3-10` and
  `main.py:474`. A reader sent to `db.py:39` finds an import block.
- `domain/ports.py:24/:26/:37/:66/:84/:96/:107/:111` — `ports.py` is now 2464 lines;
  `DocumentStore` is at ~`:1090`, `MessageBus` ~`:1102`, `SecretProvider` ~`:1113`,
  `Telemetry` ~`:1140`. Every ports line ref is from the early-slice layout.
- `deps.py:58/:62/:93` — actual `IdentityProvider` `:57`, `LocalHeaderIdentityProvider`
  `:61`, `get_current_user` `:94`. Off-by-one drift (only `deps.py:98` fail-closed is
  still exact).
- `case.py:144/:156`, `audit.py:26`, `plan.md:289/:206/:252` — unverified individually
  but same vintage; assume stale until re-pinned.
- **Fix:** either re-pin all line numbers or (recommended) drop precise line numbers in
  favor of symbol anchors (`` `get_current_user` in `app/deps.py` ``), which survive
  refactoring. Failing that, add a CI lint that checks referenced `path:line` targets
  exist.

### L-4 (low) — Module map (§3) describes the early-slice layout, not the current tree

- Lists `infrastructure/identity/local_identity_provider.py` + `current_user.py` — on
  disk `infrastructure/identity/` contains only `__init__.py` + `entra_placeholder/`;
  the local provider lives in `app/deps.py:61-83`.
- Lists `infrastructure/persistence/migrations/` — migrations live at `backend/alembic/`.
- Lists `interfaces/cli/` (Typer CLI) and `interfaces/events/local_event_handlers.py` —
  both directories exist but are **empty**; the working CLI is `app/cli/seed.py`.
- Lists `infrastructure/persistence/repositories/*` as the single repo location — there
  are now also ~20 `*_http_dependencies.py` provider modules plus `search/`, `siem.py`,
  `sla_scanner.py`, `retention_repository.py` at the same level.
- Frontend map lists `cases|tasks|documents|audit|workflow|shell/` — actual
  `frontend/src/` has no `documents/` but adds `admin|forms|imports|reports|retention|
  search|security/` (platform grew past the slice; the map did not).
- The pillar→module matrix (§3.1) and "P04/P06/P07/P08/P12/P14 not built" note (§180) are
  now under-claims: models, routers (34 files), and ADRs show those pillars have landed
  substantial surfaces.
- **Fix:** regenerate §3 from the tree (or scope it explicitly: "first-slice subset as
  of 2026-09-03" with a pointer to the live tree). Decide whether the empty
  `interfaces/cli/` + `interfaces/events/` dirs should be removed or filled — empty
  dirs that a doc points at are worse than no dirs.

### L-5 (low) — ER sketch omits columns/tables that exist; portability section is fine

- `tenants` block lists `id/name/created_at` but the table has a `key` column
  (`models.py:40`) that backs `TenantRepository.short_key` — the reference-format story
  (§16.3, ADR-024) depends on it, so it should appear.
- `users` block omits nothing critical, but newer tables (`roles`, `role_assignments`,
  `channels`, `quarantine_records`, `captured_emails`, `webhook_subscriptions`,
  `outbound_deliveries`, `saved_searches`, `search_audit`, `sla_clocks`, `queues`,
  `*_forms`, `retention_*`, `document_redactions`, `signed_audit_batches`) are absent.
  Acceptable if §4 is scoped to the first slice — but it currently claims to sketch
  "the canonical tables from plan.md §16.1" without a scope note, so a reader comparing
  ER to `models.py` sees a ~2× gap.
- §4.1 portability invariants verified accurate (generic `Uuid`/`JSON`/`DateTime(tz)`,
  no `JSONB`/GIN/`ILIKE`/`tsvector` in models; search adapters are `LIKE`-based).

### L-6 (low) — Seam tables disagree on seam #8 and newer seams

- `seams.md` row 8 marks enterprise imports "Defer until Phase 3+" with wiring "not in
  this slice", while `architecture.md` §7 row 8 says the same — consistent with each
  other, but the codebase now contains `application/imports/run_import.py`,
  `routers/imports.py`, `ImportJobRepository` port, and `imports/onbase_panzura_placeholder/`
  with a README. The seam is more "local adapter landed" than "deferred". One of the
  two (code or docs) is behind.
- Real, wired seams with no row in either 8-seam table: **search engine**
  (`DocumentSearchProvider`/`sql_search_provider.py`, marked `# INTEGRATION_SEAM: search
  engine`), **SIEM/signed-batch** (health keys + `siem.py`), **scheduler/SLA scanner**
  (`main.py:474` seam comment + `SLA_*` env contract in `seams.md` but no table row),
  **GraphQL read surface** (ADR-014 + `interfaces/graphql/`). Recommend either expanding
  to an N-seam table or adding a "landed without a row" note so seam-count claims
  ("every entry corresponds to one of the 8 seams", `main.py:424-425`) stop being
  literally false.

### L-7 (low) — `X-Dev-Tenant` header: docs over-claim what identity reads

- `architecture.md` §5 sequence ("`X-Dev-User, X-Dev-Tenant`"), §7 row 4, and the
  `deps.py:64` docstring all say the local IdP "reads `X-Dev-User`/`X-Dev-Tenant`".
- Code: `get_current_user` (`deps.py:94-121`) reads **only `x-dev-user`**; the tenant
  comes from the pre-registered `UserContext`, not from a header. `x-dev-tenant` *is*
  read, but by the idempotency middleware (`interfaces/http/idempotency.py:204`) for
  tenant-scoping the replay store, not for identity.
- Security posture is unaffected (fail-closed 401 is intact), but the docstring on the
  identity function is wrong about its own inputs and the sequence diagram implies the
  tenant is caller-supplied at identity time.
- **Fix:** correct `deps.py:64` docstring; annotate the sequence step with which layer
  consumes which header.

### L-8 (low) — POST /cases sequence shows unconditional workflow/task inserts; code is conditional

- Diagram §5 has `INSERT INTO workflow_instances` + `INSERT INTO tasks` as fixed steps.
- `create_case.py:22-26,92-98,169-175,253-275`: workflow start is via an optional
  `WorkflowStarter` bundle — "No matching workflow means 'not configured': intake
  succeeds without one." The diagram's happy path is the workflow-configured path; the
  no-workflow path (valid and tested) is not shown or footnoted.
- Related: the diagram's `prev_hash = SELECT MAX(hash)` is illustrative; the actual
  append path lives in `infrastructure/persistence/audit_events.py` — fine as
  illustration, but it should be labeled as such since M-1 already shows readers take
  the diagram literally.

### L-9 (low) — Guard/cycle section (§6) is accurate but untestable from the doc

- Operator list (`all/any/not/eq/ne/in/gt/gte/lt/lte/exists/contains/regex`) matches
  `domain/rules.py` contract; cycle-validation table matches the documented validator
  behavior. No code contradiction found.
- The doc never names the module that owns cycle validation (validator location is
  given only as "plan.md:235"), so a reviewer cannot walk the call path per R3 from
  this doc alone. Name the file.

### L-10 (low) — ADR template compliance: 4 ADRs deviate from Context/Options/Decision/Consequences

Loose scan (case-insensitive section/keyword match) over all 29 ADRs:

- ADR-005, ADR-006, ADR-007 discuss alternatives inline but have **no explicit Options
  section and no Consequences section** — they read as decision records of record, but
  they do not follow the AGENTS.md §4.R8 template the slice claims ("every material
  decision → Context/Options/Decision/Consequences/Seam note").
- ADR-024 (`Status: Proposed … awaiting re-approval`, gate `needs_revision`) has Context
  and Decision-required content but **no Consequences** — understandable for a
  non-accepted ADR, but ADR-020's index lists it without flagging that it is the one
  index entry that is *not decided*. A reader following "Decisions index" lands on a
  proposal.
- All 29 mention seams; ADR-014's numbered-heading style (`## 1. Context`, `## 2.
  Options`) is fine but means strict heading linters would false-positive — standardize
  or loosen the lint.
- **Fix:** backfill Options/Consequences for 005/006/007, flag ADR-024 as Proposed in
  the ADR-020 index row, and record the accepted heading variants somewhere CI can see.

### L-11 (low) — `check_architecture_md.py` enforces less than R9 asks

- The script checks *existence + one Mermaid fence* for immediate `specs/NNN-*` children.
  It does not check: fences render (a fence containing prose passes), required diagram
  *types* (flowchart + ER + sequence per §13/R9), the module map, the seam table, or
  `specs/pillars/*` docs. That matches its docstring, so it is not buggy — but
  `architecture.md` §12 and ADR-020 describe it as the thing that makes "closing a
  slice without architecture.md mechanically impossible", which over-claims: it makes
  closing without *a file containing the string* ```mermaid mechanically impossible.
- Only 1 slice (`001-case-management-platform`) is discovered — correct today, but note
  `specs/pillars/` siblings are (correctly) ignored; a future `specs/002-*` with a typo
  (`02-*`, `N-*`) would be silently skipped rather than failed. Consider failing on
  non-matching directories that contain `spec.md`/`plan.md`.
- **Fix:** doc wording + optionally assert minimum fence count (this doc has 6; require ≥3)
  and the `erDiagram`/`sequenceDiagram` keywords §13 mandates.

### L-12 (low) — DoD checklist (§12) is all-unchecked with no status pointer

- All 9 boxes are `[ ]`, which is correct for an open gate — but there is no pointer to
  *where* gate status lives (tasks.md AC-001..AC-010, CI runs) nor a last-verified date.
  `Last updated: 2026-09-03` is a week stale relative to this review and predates
  ADR-029. Bump the date and link the gate evidence when the slice closes.

### L-13 (low) — Minor: `seams.md` wiring column has two stale cells

- Row 1 wiring `app/db.py:39, app/deps.py:get_db` — `db.py:39` is the same stale ref as
  L-3 (no marker there); `get_db` itself is accurate (`deps.py:124`).
- Row 3 wiring `app/main.py:_startup` — the function is named `_startup` (nested in
  `create_app`, `main.py:413`), so the ref resolves, but the leading-underscore nested
  name is fragile as an anchor; prefer `create_app › _startup`.
- Env-var contract table is otherwise accurate against `config.py` (all names verified:
  `APP_ENV`, `DATABASE_URL`, `DOCUMENTS_PATH`, `SECRET_PROVIDER`,
  `SECRETS_LOCAL_STORE_PATH`, `LOG_LEVEL`, `TELEMETRY`, `TENANT_LISTENER_ENABLED`,
  `SLA_*`; Azure `AZURE_*` vars correctly appear only in placeholder READMEs, never in
  `Settings` — good fail-closed hygiene).

---

## 3. Recommendations (no code changed per instructions)

1. Fix **M-1** (hash separator) and **M-2** (ADR-020 index) first — both are
   single-edit doc fixes with outsized reader impact.
2. Adopt the health-endpoint label vocabulary everywhere (L-1) and re-pin or remove
   `file:line` refs (L-3) in the same pass — both are search-replaceable.
3. Regenerate the §3 module map from the tree and scope it ("first-slice subset" vs
   "current"), resolving the empty `interfaces/cli/` + `interfaces/events/` question (L-4).
4. Split `models.py` or record the R2 exception via ADR (L-2) — the only finding that is
   a live violation of a hard rule rather than doc drift.
5. Extend the seam inventory to cover search/SIEM/scheduler/GraphQL or explicitly scope
   it to "the 8 Azure-swap seams" (L-6); backfill ADR Options/Consequences (L-10);
   tighten the R9 gate wording (L-11).
6. Re-run this review after the next slice: the drift pattern (docs describe slice N,
   code is at slice N+1) will recur unless line refs become symbol refs and the module
   map/ER get a "regenerate on converge" step in the slice checklist.

## 4. Files reviewed

- `specs/001-case-management-platform/architecture.md` (733 lines, 6 Mermaid blocks)
- `docs/integrations/seams.md` (99 lines) + all 8 `*_placeholder/README.md`
- `docs/decisions/ADR-*.md` (29 files, ADR-001…ADR-029)
- `scripts/check_architecture_md.py` (92 lines; executed — gate OK)
- Code cross-checks: `backend/app/main.py`, `config.py`, `db.py`, `deps.py`,
  `domain/ports.py` (2464 lines), `domain/audit.py`, `application/cases/create_case.py`,
  `interfaces/http/routers/health.py`, `cases.py`, `interfaces/http/pagination.py`,
  `interfaces/http/idempotency.py`, `infrastructure/persistence/models.py` (1611 lines),
  `infrastructure/*_placeholder/`, `frontend/src/api/types.gen.ts`,
  `frontend/src/shell/Banner.tsx`, `specs/pillars/00-product-roadmap.md` §7
