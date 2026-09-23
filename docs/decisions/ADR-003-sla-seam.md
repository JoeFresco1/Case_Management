# ADR-003: SLA clock and legal-hold refusal seams

- Status: Accepted (2026-09-03)
- Scope: `backend/app/domain/sla.py`,
  `backend/app/domain/case.py` (legal_hold flag),
  `backend/app/domain/ports.py` (`SlaClockRepository`),
  `backend/app/domain/audit.py` (`SLA_BREACHED` event),
  `backend/app/application/sla/scan_sla.py`,
  `backend/app/infrastructure/persistence/models.py`
  (`sla_clocks` table, `cases.legal_hold` column),
  `backend/alembic/versions/0013_create_sla_clocks_and_legal_hold.py`.
- Supersedes: nothing.
- Related: AGENTS.md §12 (seam inventory), §4.R12 (security first,
  deny by default), §16.1 (canonical tables), §17.1 (HTTP API
  shape), `specs/pillars/P02-workflow-tasks-queues/pillar.md` §3
  (`SlaClock` first slice), `specs/pillars/P08-records-retention/
  pillar.md` §3 (`case.legal_hold` first slice),
  `specs/pillars/01-deduplicated-feature-map.md:62` (FM-018),
  `:98` (FM-039), `specs/001-case-management-platform/first-slice/
  tasks.md:139` (T068/T069), `plan.md:206` (audit hash chain in the
  same transaction), `plan.md:289` (portable types only).

## Context

Two pillars ship seams in the first slice that the rest of the
codebase does not yet exercise end-to-end:

- **P02 / FM-018** ships a basic SLA clock (deadline + breach). The
  pillar doc puts the first slice in the "H0 seam" bucket — the
  clock lifecycle, the breach action, and the audit + timeline
  visibility are in; advanced queue / notification / escalation
  policies land in H1 (`FM-018` "H1 (basic seam in H0)" in
  `01-deduplicated-feature-map.md:62`).
- **P08 / FM-039** ships a legal-hold *refusal* seam — a single
  binary `case.legal_hold` flag that blocks any attempt to close a
  case while the flag is `True`. The full hold lifecycle (per-case
  hold record with actor / reason / released_at / released_by) is
  H1 (`FM-039` "H1 (refusal seam in H0)" in `01-deduplicated-
  feature-map.md:98`).

The two seams share a transaction shape — domain change + audit
row + outbox event in one commit (Constitution III, `plan.md:206`)
— and a security posture: deny by default, fail-closed, no silent
overwrite (`AGENTS.md §4.R12`). They also share the same H0
discipline: ship the smallest possible surface that lets a future
slice build the full feature without re-plumbing the persistence
layer.

This ADR records three concrete decisions:

1. The SLA clock is an independent aggregate, not a column on the
   case or the task.
2. The legal-hold refusal lives on the `Case` aggregate, not in a
   separate `legal_holds` table.
3. The breach scanner is an application use case, not a SQL
   trigger or a database job.

## Options considered

### For the SLA clock

1. **Column on the `cases` row** (`due_at`, `breach_status`).
   - Pros: zero new table, zero new join, simplest possible read.
   - Cons: a case with three tasks (each with its own deadline)
     would need three columns or a JSON blob; either is a layering
     violation (P01 owns the case, P02 owns the deadline). Audit
     rows would compete with case edits on the version token; the
     legal-hold refusal would block the case-level deadline but
     not a task-level one.
2. **JSON column on the case** (`cases.sla_clocks_json`).
   - Pros: no new table.
   - Cons: JSON-stored aggregates defeat the AGENTS.md §3 R1 ban
     on JSONB and the §16.1 portable-types rule; queries for the
     scanner become a full table scan; concurrent updates to the
     same case's clocks serialize on the case row.
3. **Independent `sla_clocks` table + `SlaClock` aggregate**
   (chosen).
   - Pros: matches the domain decomposition (P02 owns the clock,
     P01 owns the case). Tenant-scoped queries are cheap with the
     composite index on `(tenant_id, status, due_at)`. The
     scanner is a single, narrowly-typed read primitive
     (`list_due`) that the SQL adapter can serve without
     application-layer filtering. Per-task and per-case clocks
     share the same shape and the same breach audit code.
   - Cons: one extra table, one extra Protocol. The Protocol is
     four methods (`add`, `get`, `update`, `list_due`); the
     adapter is a thin SQLAlchemy mapping.

### For the legal-hold refusal

1. **Per-case `legal_holds` table** (full lifecycle, H1 scope).
   - Pros: full provenance (actor, reason, timestamps, chain of
     custody).
   - Cons: a full hold record needs an admin endpoint, a hold
     release flow, and audit rows for the place / release actions
     — none of which are first-slice scope. The H0 refusal only
     needs to read *one* bit, "is this case held?", at the point
     of the disposition.
2. **Boolean flag on `cases`** (chosen).
   - Pros: trivially readable; the aggregate's
     :meth:`Case.change_status` is the single point of refusal so
     the workflow engine, the admin tool, and a future bulk
     importer all inherit the same guard; the column is
     `NOT NULL DEFAULT FALSE` (metadata-only on PostgreSQL ≥ 11)
     so the migration is cheap and reversible.
   - Cons: the flag carries no provenance. H1 will add a
     per-case `legal_holds` table; the `cases.legal_hold` column
     becomes a denormalized read-side cache the application layer
     maintains. The aggregate keeps the guard so the H0 contract
     ("a held case cannot be closed") is enforced at the same
     code point across slices.

### For the breach scanner

1. **SQL trigger** on `sla_clocks` that inserts an `SlaBreached`
   audit row when `due_at` is crossed.
   - Pros: zero application code, "fire and forget".
   - Cons: a trigger runs in the database's transaction; the audit
     row's hash chain requires the previous row's `hash` and
     `prev_hash`, which the trigger would have to read with a
     sub-query — fragile under concurrent inserts. The trigger
     also cannot dispatch the configured `breach_action`
     (create_task) in the same transaction without violating the
     "audit row written by the application layer" rule from
     `plan.md:252`. Triggers are also a portability hazard
     (AGENTS.md §3 R1, §4.R1) — a PostgreSQL trigger does not run
     on Azure SQL with identical semantics.
2. **Database job** (`pg_cron`, SQL Server Agent).
   - Pros: declarative schedule, OS-level reliability.
   - Cons: another moving part on the operator console; the H0
     scanner is small enough to live in the application; the
     production target swaps the local loop for a hosted worker
     (`AGENTS.md §12` — scheduler is the seam, not the
     application).
3. **Application use case `app.application.sla.scan_sla.scan_once`**
   (chosen).
   - Pros: pure Python, runs identically on SQLite (tests) and
     PostgreSQL / Azure SQL (production); the use case is the
     single integration point (AGENTS.md §12); the audit row is
     written by the application layer in the same transaction as
     the clock update, satisfying the hash-chain contract.
   - Cons: requires a scheduler. The local adapter starts the
     loop in `main.py` next to the outbox dispatcher; the
     production adapter runs the same use case from a hosted
     worker.

## Decision

Adopt the three chosen options above with the following concrete
rules:

1. **`SlaClock` aggregate + `sla_clocks` table.** The clock has
   its own lifecycle (`running` / `paused` / `breached` /
   `completed`), its own audit row type (`SlaBreached`), and its
   own repository Protocol
   (`SlaClockRepository` in `app.domain.ports`). The pause /
   resume math credits the pause duration so a paused clock
   resumes with the same budget remaining — no silent time loss
   or accrual. `mark_breached` is idempotent at the domain layer
   so the scanner can re-tick without double-firing.

2. **`cases.legal_hold` boolean column.** The flag is the
   single signal the application layer needs to refuse a
   disposition; the refusal lives in
   `Case.change_status` so every code path that flips a case to a
   terminal status inherits the guard. The error maps to HTTP
   409 `conflict` at the boundary (per `AGENTS.md §10` and the
   existing `interfaces/http/errors.py` mapping). H1 will add a
   per-case `legal_holds` table; the column becomes a
   denormalized read-side cache.

3. **`scan_sla.scan_once` as the breach runner.** The use case
   is per-tenant, idempotent, and portable. The scanner asks
   the case whether it is under legal hold so a held case's
   clocks still record the breach in the timeline but the
   configured `breach_action` does not fire — the operator sees
   the deadline was missed and the disposition was blocked.
   H1 will replace the boolean seam with the per-case hold
   record; the application use case keeps the same shape.

4. **Breach actions in v1.** Two shapes are honored at the
   scanner: `{"type": "create_task", "queue": "<queue_key>",
   "task_type": "..."}` (materializes a `Task` on the case) and
   `{"type": "notify", "queue": "<queue_key>"}` (a no-op in
   H0 — the `SlaBreached` event itself is the notification; a
   future slice may fan it out to email / IM). The publish-time
   validator rejects unknown shapes; runtime is
   defense-in-depth only.

5. **Outbox + audit in one transaction.** The scanner's
   `clock_repo.update`, `audit.append`, and `outbox.enqueue`
   share the caller's transaction so a partial commit never
   leaves a clock in `breached` without its audit row. The
   outbox dispatcher drains the row through the same pipeline
   the workflow engine uses (FM-055).

6. **Idempotency at three layers.** The scanner is safe to run
   at any cadence: the aggregate's `mark_breached` is the
   first dedup, the audit layer's `record_event` is the
   second, and `list_due` filters to `status='running'` so a
   clock the scanner already processed is not revisited. A
   backdated `now` (e.g. the verify-audit CLI) does not
   double-write rows.

## Consequences

### Positive

- A worker sees SLA deadlines in the case workspace and a breach
  surfaces in the timeline in the same transaction as the case's
  other domain events (P02-US6 acceptance scenario B6).
- A held case cannot be closed by the workflow engine, the admin
  tool, or a future bulk importer; the refusal is at the
  aggregate so the rule is enforced uniformly (P08-US1 acceptance
  scenario H1).
- The SLA scanner is a single use case the operator can dry-run
  from a CLI (a future `app.cli.scan_sla` mirrors the
  outbox dispatcher's `drain_once` pattern). The hash chain is
  preserved because the audit row is written by the application
  layer in the same transaction as the clock update.
- The seam is portable: the same domain aggregate runs on
  SQLite (tests) and PostgreSQL / Azure SQL (production)
  because all columns are SQLAlchemy generic types
  (`Uuid`, `JSON`, `DateTime(timezone=True)`, `String`,
  `Boolean`).

### Negative / risks

- The `sla_clocks` table adds a new query path; the production
  worker must keep its tick interval short enough that breach
  latency is bounded. The default of 60 s is sized for the
  local profile; production may run a faster loop.
- The `legal_hold` boolean carries no provenance. H1 must
  add a per-case `legal_holds` table and the application layer
  must maintain the boolean as a denormalized cache; the
  aggregate's guard stays in place.
- The scanner asks the case via a `Protocol` (`_CaseLookup`).
  H1 may need to widen the seam (e.g. to include the hold
  reason for the audit payload) — a small adapter change, not
  a domain rewrite.

## Azure-seam note

The scanner's loop is the AGENTS.md §12 seam. The local
adapter starts the loop in `main.py` next to the outbox
dispatcher; the production target runs the same use case
(`app.application.sla.scan_sla.scan_once`) from a hosted worker
(Azure Functions / Container Apps) consuming the same `sla_clocks`
table. No domain or application change is required to swap the
scheduler. The breach audit row's hash chain is preserved
across the swap because the application layer — not the
scheduler — writes the row.
