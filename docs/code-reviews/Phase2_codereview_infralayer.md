# Phase 2 Code Review — Infrastructure Layer (`app/infrastructure`)

> Scope: `backend/app/infrastructure/**`, `backend/app/db.py`, `backend/app/config.py`,
> `backend/alembic/versions/**`, `backend/alembic/env.py`, plus the port contracts in
> `backend/app/domain/ports.py` they implement.
> Method: read-only review. No fixes applied. All findings are observations with `file:line`
> references for the owner to triage.
> Date: 2026-09-10. Checked against AGENTS.md R1 (portable Postgres only), R2 (≤1000 lines),
> R3 (skepticism), R10 (layer boundaries), R12 (security/audit), §12 (seams), §16 (schema).

## 0. Verdict summary

| Area | Verdict |
|---|---|
| Port implementations | **Fail** — 2 material gaps (Queue repo surface, Case `list_open_for_queues` stub) + 1 critical DocumentStore `get` bug + mapping drops `legal_hold` |
| Tenant isolation in queries | **Pass with observations** — all domain-data reads are explicitly scoped; worker/operator paths are intentionally global but `replay()` lacks a tenant check |
| Audit logging integration | **Pass with gaps** — chain + dedupe + timeline reads are correct; raw-SQL mapper bypass relies solely on the DB trigger (execute-listener is a no-op stub) |
| Migration portability | **Pass with Azure-seam risks** — generic types everywhere, `upgrade`+`downgrade` everywhere; trigger DDL + `tuple_()` pagination + `SignedAuditBatch` drift will break the Azure-SQL target |
| Raw SQL dialect usage | **Pass (local)** — no `JSONB`/`ILIKE`/`tsvector`/`gen_random_uuid()` in DDL/DML; only documented trigger DDL + portable `LIKE`/`func.lower` remain |

Severity key used below: `CRITICAL` (data loss / security / total breakage), `MAJOR` (contract violation, must fix before slice-done),
`MINOR` (robustness / drift / future-target risk), `NOTE` (positive confirmation).

---

## 1. Port implementations (`app/domain/ports.py` vs adapters)

### 1.1 `CaseRepository` — `SqlCaseRepository` — MAJOR (stub)

- File: `backend/app/infrastructure/persistence/repositories/case_repository.py:140-164`
- Port requires `list_open_for_queues` (`backend/app/domain/ports.py:137-153`, P12-US1/FM-051,
  `WHERE tenant_id`, `status='open'`, `owner_queue_id IN`, ordered `updated_at DESC`).
- Implementation unconditionally `raise DependencyUnavailable("...not yet wired (T063 / P12 H1)")`.
- Everything else in the class is correct and well-scoped:
  `add` (`:44`), `update` compare-and-swap on `version_token - 1` (`:47-76`),
  `get` (`:78-81`), `get_by_reference` (`:83-88`), `list` (`:90-105`),
  `next_reference` portable `LIKE` + per-tenant unique index (`:107-138`).
- Docstring is honest about the stub, but callers (reports router) can only surface 503.
  Either the P12 scope must exclude this method or T063 must land before slice-done.

### 1.2 `QueueRepository` — `SqlQueueRepository` — MAJOR (5/6 methods missing)

- Port: `backend/app/domain/ports.py:528-591` requires
  `list_keys`, `list_queues`, `get`, `get_by_key`, `add`, `update`, `delete`.
- Implementation: `backend/app/infrastructure/persistence/repositories/workflow_repository.py:197-213`
  provides **only** `list_keys` (via `Queue.__table__.c.key` / `c.tenant_id` — itself tenant-scoped, good).
- `list_queues`, `get`, `get_by_key`, `add` (409-on-conflict), `update` (CAS on `version_token`),
  `delete`/retire are absent. Any queue-admin use case beyond key-membership checks has no SQL backing.
- Positive: `SqlWorkflowDefinitionRepository` (`:30-86`) and `SqlWorkflowVersionRepository`
  (`:89-194`) fully cover their protocols (`get`/`get_by_key`/`set_current_version`,
  `get`/`add`/`get_latest_for_workflow`/`get_by_version_number`/`mark_status` with strict
  `draft→published→retired` guard at `:177-189`). Extra `add`/`list`/`list_versions` helpers are
  additive, not violations.

### 1.3 `DocumentStore` — `LocalDocumentStore.get` — CRITICAL (always fails)

- Port: `backend/app/domain/ports.py:1090-1099` — `put(tenant_id, content, content_type, sha256) -> str`,
  `get(tenant_id, content_ref) -> bytes`.
- `put` returns a **relative path** (`backend/app/infrastructure/storage/local_document_store.py:106`:
  `str(path.relative_to(self._root))`, e.g. `<tenant>/<shard>/<sha>`).
- `get` passes that value straight into `_resolve_path` (`:115`), which validates
  `is_valid_sha256(sha256)` (`:129-142`) and raises `DependencyUnavailable` for anything that is
  not 64-char lowercase hex. A stored `content_ref` therefore never validates.
- Net effect per current code: every `get` of a previously-`put` document raises
  `DependencyUnavailable("document store ref is not a valid sha256")`. Path-traversal guard
  itself (`:148-156`, `resolve()` + `relative_to(root)`) is well done — the bug is the
  `ref`-vs-`sha` type confusion, not the guard.
- `put` atomicity (`:87-93` tmp + `os.replace`), sha pre-validation (`:78-83`), and
  `DependencyUnavailable` mapping (`:94-101`, `:122-125`) are otherwise correct.

### 1.4 `MessageBus` — signature drift — MINOR

- Port: `backend/app/domain/ports.py:1102-1110` — `publish(tenant_id, event_type, event_id, payload)`.
- Adapter: `backend/app/infrastructure/messaging/local_message_bus.py:94-104` accepts three extra
  **optional** kwargs: `correlation_id`, `causation_id`, `occurred_at`.
- Caller: `backend/app/infrastructure/messaging/outbox_dispatcher.py:217-225` passes all three.
- Structurally compatible today (extra optionals don't break `Protocol` assignment at runtime),
  but the port no longer documents the wire envelope the dispatcher depends on
  (`_ENVELOPE_*` keys at `outbox_dispatcher.py:81-96`). Either widen the port or move the
  envelope handling into the dispatcher so adapters stay interchangeable with Azure Service Bus.

### 1.5 `SecretProvider` / `Telemetry` / search providers — NOTE (pass)

- `LocalSecretProvider` (`backend/app/infrastructure/secrets/local_secret_provider.py:61-80`)
  matches `get(name) -> str` / `set_secret(name, value)` (`ports.py:1113-1137`), env-first then
  JSON file, atomic write (`:103-106`), fail-closed on missing/non-dict store (`:69-73`, `:90-94`).
  No secret values logged. Correct.
- `SqlSearchProvider` (`search/sql_search_provider.py:59-126`) subclasses the `SearchProvider`
  port, carries `WHERE tenant_id` on every query (`:88`), uses only `func.lower().like()` +
  `LIKE`-on-JSON-text (`:152-220`), explicitly rejects `ILIKE`/`JSONB`/`tsvector` (`:1-13`).
  LIKE metacharacters escaped (`:223-225`). Correct per R1.
- `SqlDocumentSearchProvider` (`search/sql_document_search_provider.py:60-145`) likewise subclasses
  its port, scopes `tenant_id + case_id` (`:83-86`), enforces classification allowlist (`:109-110`)
  and sensitivity threshold (`:115-122`) at SQL, redacts snippets (`:148-182`). Correct.
- `SqlTaskRepository` (`repositories/task_repository.py:19-136`) covers the full `TaskRepository`
  port (`add`/`update` CAS `:29-45`, `get`, `list_for_case`, `list_open_for_queue`,
  `list_open_for_user`, `list_for_actor`, `list_overdue_for_queues` with `due_at IS NOT NULL`
  guard `:130`). All reads scoped. Correct.
- `SqlWorkflowInstanceRepository` + `SqlWorkflowTransitionLog`
  (`repositories/workflow_instance_repository.py:35-150`) cover `add`/`get`/`get_for_case`/
  `update` (CAS `:77-92`) / `bump_state` (CAS `:94-125`) / `record`. All scoped. Correct
  (note: they map to `app.application.workflow.start_workflow.WorkflowInstance`, not a domain
  entity — layering smell, see §5.3).
- `SqlSlaClockRepository` (`repositories/sla_clock_repository.py:92-217`) scopes `add`/`get`/
  `update` and (per remainder of file) `list_counting`/`list_due`/`list_for_case`. The
  `metadata_json` stuffing of `warning_at`/`escalation_*` (`:59-89`) round-trips but is lossy by
  design (H0 columns) — documented, acceptable, but H1 must add real columns rather than
  extend the stuffing.

### 1.6 `mapping.py` drops `Case.legal_hold` — MAJOR (silent data loss)

- Model: `backend/app/infrastructure/persistence/models.py:148` — `Case.legal_hold: bool`
  (`nullable=False, default=False`, FM-039 H0 seam).
- Mapping: `backend/app/infrastructure/persistence/mapping.py:152-189`
  (`case_to_domain` / `case_from_domain`) contains **no** `legal_hold` key in either direction.
- Consequence: the flag is persisted as `False` on insert and never hydrated on read; a case
  placed under legal hold via any path that round-trips through this mapper silently loses the
  hold. If the domain `Case` does not yet carry the field, the column is write-only dead schema;
  if it does, this is a correctness bug. Either way the mapper and model disagree today.

### 1.7 `SqlIdempotencyStore.put` never writes `expires_at` — MINOR

- Model column exists: `models.py:432` (`expires_at`, added by migration `0006`, "retention
  tooling can GC without a schema change").
- Writer: `audit_outbox.py:508-543` persists `tenant_id/key/request_hash/response_status/
  response_body/correlation_id/created_at` but never `expires_at` (always NULL).
- Consistent with the middleware not enforcing expiry yet (documented in the model comment),
  but the retention story is then aspirational: no writer, no reader, no janitor. Fine for the
  slice if explicitly deferred; otherwise set a default TTL on `put`.

### 1.8 Production module imports test fakes — MINOR (layering smell)

- `backend/app/infrastructure/persistence/audit_outbox.py:568-575` — module-level `__getattr__`
  lazily imports `InMemoryIdempotencyStore` from `tests.application.cases._fakes`.
- Works (and is `pragma: no cover`), but production code now has a hard import edge to `tests/`.
  The historical import-compat shim should move to a `conftest` alias or be removed once callers
  migrate, rather than living in the shipped adapter.

---

## 2. Tenant isolation in queries

### 2.1 Domain-data adapters — NOTE (pass)

Spot-checked every `select`/`update`/`delete` in the persistence adapters; all carry an explicit
`tenant_id` predicate and none rely on the listener alone:

- `case_repository.py:65,79,84,99,126` (incl. CAS `update`); `documents.py:173-176,191-194,241-244,
  263-267,283-287,312-316`; `audit_outbox.py:123,217,235,275-276,325,353-356,372-376,413,494-497,
  520-523`; `task_repository.py:37,49,57,69,85,104,127`; `workflow_repository.py:39-40,50,65,
  79-80,99,114-117,133-135,150-152,169-170,207`; `workflow_instance_repository.py:61,71,81,106,121`;
  `case_type_repository.py:79-80,86,97,112-114` (+ version rows `:143+`); `participant_repository.py:28-29,
  38,47-49`; `role_repository.py:57,62,67,97,106` (+ assignments remainder); `sla_clock_repository.py:110-113`;
  both search providers (`sql_search_provider.py:88`, `sql_document_search_provider.py:83-86`).
- `SqlParticipantRepository.update` (`participant_repository.py:43-53`) **is** tenant-scoped but
  does not check `rowcount` — a cross-tenant/unknown id is a silent no-op, whereas
  `SqlCaseRepository.update` (`case_repository.py:75-76`) and `SqlTaskRepository.update`
  (`task_repository.py:44-45`) raise `Conflict` on zero rows. Inconsistent failure semantics;
  recommend a uniform not-found/conflict contract (MINOR).
- Defense-in-depth listener (`backend/app/db.py:85-142`) is correctly opt-in
  (`settings.tenant_listener_enabled`, default `True` in `config.py:75`), per-session pinned via
  `set_current_tenant` (`db.py:71-82`), no-op when unpinned (`:124-126`), and best-effort
  (`suppress(Exception)` at `:136-139`). Docstrings in `db.py:1-27` and `ports.py:7-29` both state
  it is never the sole mechanism. Correct.

### 2.2 Worker / operator paths without tenant predicates — NOTE (intentional, with one gap)

- `OutboxDispatcher.drain_once` (`outbox_dispatcher.py:171-178`), `count_lag` (`:276-284`),
  `list_pending` (`:309-317`) query `OutboxRow` with **no** `tenant_id` filter. This is correct for a
  single-process background worker that drains all tenants, but it means these methods are
  operator-scope, not tenant-scope, and must never be exposed per-tenant without a filter.
- Gap (MINOR): `OutboxDispatcher.replay(row_id)` (`:319-344`) looks up by bare primary key
  (`session.get(OutboxRow, row_id)`, `:334`) with no `tenant_id` argument. An operator (or a
  mis-wired admin endpoint) that passes another tenant's row id replays cross-tenant work with
  no check. Recommend `replay(*, tenant_id, row_id)` with a scoped `WHERE`, matching every other
  port method.
- `SqlSlaScanner.tenant_ids` (`sla_scanner.py:44-48`) enumerates all tenants, then `scan_tenant`
  (`:50-105`) re-scopes every repo call by the passed `tenant_id`. Correct pattern.
- Larger concern (MINOR, seam): `SqlSlaScanner.__init__` takes an `InMemoryLegalHoldRepository`
  (`sla_scanner.py:35-40`, `legal_hold_repository.py:1-101`). The production scanner therefore
  consults **process memory** for legal holds while everything else is durable SQL. A restart or
  multi-replica deploy loses holds. The module is honest that the SQL adapter is deferred, but
  the scanner wiring should be flagged as non-production until the durable adapter lands.

---

## 3. Audit logging integration

### 3.1 Chain, dedupe, reads — NOTE (pass)

- `SqlAuditEventStore.append` (`audit_outbox.py:171-207`): resolves per-tenant `prev_hash` via
  `_last_hash` (`:209-222`, `ORDER BY occurred_at DESC, id DESC LIMIT 1`, genesis fallback),
  computes `compute_hash(prev_hash, payload)` and `payload_hash(payload)`, stages one
  `AuditEventRow` on the caller's session. Same-transaction semantics (caller commits audit +
  domain + outbox together) match the documented contract (`audit_outbox.py:11-16`).
- Dedupe key is the full 4-tuple `(tenant_id, event_type, correlation_id, payload_hash)`
  (`:115-136`, with the why-comment at `:126-130`). Correct — the chain `hash` cannot serve as
  the dedupe key since it binds `prev_hash`.
- Reads (`list_for_case` `:241-299`, `list_for_export` `:301-339`, `get_event` `:341-357`,
  `list_for_chain` `:224-239`, `list_access_decisions` `:382-441`) are all tenant-scoped,
  case-scoped where applicable, cursor-paginated on `(occurred_at, id)`. Good.

### 3.2 Four-layer append-only — MAJOR gap on the raw-SQL path

1. **DB trigger (Layer 1) — present, dialect-branched.** Migration `0002`
   (`alembic/versions/0002_create_audit_outbox.py:91-173`) installs `BEFORE UPDATE/DELETE`
   triggers for sqlite (`RAISE(ABORT)`) and PostgreSQL (plpgsql `RAISE EXCEPTION ... check_violation`),
   selected at runtime via `op.get_bind().dialect.name` (`:284-293`), dropped symmetrically on
   downgrade (`:296-308`). Error code `AUDIT_EVENTS_APPEND_ONLY` matches the Python exception
   (`audit_events.py:58`). Correct for the two supported dialects; see §4.3 for the Azure gap.
2. **ORM listener (Layer 2) — half present.** Mapper-level `before_update`/`before_delete`
   that raise `AuditEventImmutable` (`audit_events.py:68-96`) is real and wired from the
   composition root (`app.main`) / seed CLI rather than `app.db` (T039 note at `db.py:165-171`).
   However a raw `session.execute(update(AuditEvent))` bypasses the unit-of-work and never fires
   the mapper listener. The guard for that path, `_execute_state_guard` (`audit_events.py:118-140`),
   exists but `install_audit_immutability_execute_listener` (`:142-168`) is an explicit **no-op
   stub returning `True`**. The only thing stopping a raw-SQL audit mutation today is the DB
   trigger. Recommend either implementing the `do_orm_execute` hook or downgrading the
   "four layers" claim to "three + trigger" until it exists.
3. **Application has no mutation path (Layer 3) — present.** `SqlAuditEventStore` exposes only
   `append`/reads; no `update`/`delete` exists on the class (`audit_outbox.py:153-441`). Correct.
4. **Compensating corrections (Layer 4) — present in shape.** `AUDIT_CORRECTION_RECORDED` flow
   via `get_event` (tenant-scoped existence check `:341-357`) + `latest_correction_id`
   (`:359-380`) is wired. Correct as far as the infrastructure layer can show; end-to-end
   correction semantics belong to the application review.

### 3.3 `list_access_decisions` JSON filter — MINOR (brittle + injection-adjacent)

- `audit_outbox.py:435-438`:
  `stmt.where(cast(AuditEventRow.payload, String).like(f'%"{action}"%'))`.
- Two issues: (a) `action`/`result` are interpolated into the LIKE pattern with **no escaping**
  of `%`/`_` (contrast `_escape_like` in both search providers) and no parameter binding, so a
  value containing LIKE metacharacters widens the match; (b) substring match on serialized JSON
  can false-positive (`"action":"read"` matches `"subaction":"bread"`). Portable (no JSONB
  operators — good for R1), but should use bound parameters + escaping, or at minimum document
  the false-positive rate. The `action`/`result` values are enum-like today, so blast radius is
  small — still worth a follow-up.

---

## 4. Migration portability & raw SQL dialect usage

### 4.1 Types — NOTE (pass)

- All 20 migrations under `backend/alembic/versions/` expose both `upgrade()` and `downgrade()`
  (verified by enumeration: `0001`–`0019` incl. `0009a`, `0015`–`0019` reconciliations — all
  `upgrade=True downgrade=True`).
- DDL uses only generic types: `sa.Uuid()`, `sa.JSON()`, `sa.DateTime(timezone=True)`,
  `sa.String`, `sa.Text`, `sa.Integer`, `sa.Boolean` (spot-checked `0001`–`0014`, `0018`, `0019`).
- No `JSONB`, `GIN`, `ILIKE`, `tsvector`, `gen_random_uuid()`, array/range types in any migration
  body. The only hits for those tokens are **comments** explaining their absence plus the
  `env.py` lint guard (`alembic/env.py:50-64`) that fails the migration if a banned token appears.
  That guard is a genuine strength — keep it.
- `server_default` values are portable literals: `sa.false()`/`sa.true()` (`0001:91`, `0005:274`,
  `0007:71`, `0009a:43`), `"0"`/`"1"`/`""` (`0002:268`, `0003:201`, `0005:94`), `sa.text("0")`
  (`0002:268`). `0014:68` uses `sa.text("'[]'")` — a quoted-string literal default that is
  portable in practice on sqlite/postgres but is the most dialect-sensitive default in the set;
  consider a Python-side default instead if Azure SQL complains.

### 4.2 Raw SQL — two sanctioned sites, one unannotated

- **Sanctioned (documented):** `0002` trigger DDL via `op.execute(...)` (`0002:100-172`). Each
  statement carries a `# portability:` comment and the dialect branch is explicit (`:284-293`).
  This is exactly the "raw dialect SQL unavoidable → annotate" case AGENTS.md allows. No change
  requested for sqlite/postgres.
- **Unannotated:** `0015_reconcile_workflow_runtime.py:19` —
  `op.execute("UPDATE workflow_instances SET version_token = 1")`. The statement itself is
  portable, but it ships with no portability comment and no `WHERE` scoping note (it backfills
  every row). Add the one-line comment for consistency; no behavioral issue.
- No `sa.text()` fragments with dialect functions, no `func.now()` server defaults, no
  `CREATE EXTENSION`, no `postgresql_*` kwargs anywhere in `alembic/versions/`. Clean.

### 4.3 Azure-target risks (not local failures — flag for the seam backlog)

1. **Trigger migration hard-fails on any third dialect** (MINOR, future).
   `0002:291-293` and `downgrade` (`:306-308`) `raise RuntimeError` for `dialect not in
   ("sqlite", "postgresql")`. Azure SQL (mssql/TDS) therefore cannot run the migration chain at
   all. Expected — the trigger needs a T-SQL `INSTEAD OF` equivalent — but the failure mode is a
   crash, not a skip. Track as an Azure-seam item with the T-SQL trigger body reserved.
2. **`tuple_()` row-value pagination is not T-SQL portable** (MINOR, future).
   `audit_outbox.py:294-296, 334-336, 426-428` paginate with
   `tuple_(occurred_at, id) > tuple_(cursor_at, cursor_id)`, commented as portable on
   "sqlite + postgres + azure-sql" (`:292-293`). T-SQL does **not** support row-value
   comparison. The sqlite/postgres claim is fine; the azure-sql claim needs revisiting
   (rewrite as `(occurred_at > :at) OR (occurred_at = :at AND id > :id)` before the Azure
   adapter lands).
3. **`SignedAuditBatch` model without migration = schema drift** (MINOR).
   `models.py:329-375` defines `signed_audit_batches` with the comment "no DDL is emitted ...
   Migrations land with the Azure adapter". Any context using `Base.metadata.create_all`
   (notably the sqlite test profile) **will** create the table, while `alembic upgrade head`
   will **not**. Tests and prod therefore run different schemas. Either add the migration now
   (with a `Gated` note) or exclude the model from `Base.metadata` until the migration exists.
4. **`OutboundDelivery.occurred_at` typed as `String(64)`** (MINOR).
   `models.py:934` maps `occurred_at: Mapped[str | None]`. Every sibling timestamp is
   `DateTime(timezone=True)`. If the domain value is a datetime, this forces stringly-typed
   comparisons and ordering bugs; if it is genuinely a wire string, rename to
   `occurred_at_raw`/`wire_occurred_at` so the next reader doesn't "fix" it into a datetime.

### 4.4 Models file size — MAJOR (R2 hard limit)

- `backend/app/infrastructure/persistence/models.py` is **1611 lines** (tallest in the layer;
  next: `audit_outbox.py` 575, `mapping.py` 495). AGENTS.md R2 sets a **hard limit of 1000
  lines** with refactor pressure at 600. The file aggregates ~20 entities (tenants → SLA clocks →
  webhooks → saved searches) that the plan already groups by bounded context. Split by aggregate
  (`models_core.py`, `models_documents.py`, `models_workflow.py`, …) with a re-export shim, then
  re-run `ruff` + `import-linter` as R2 requires. Content itself is portable (generic types only,
  `Tenant` FK + indexes on every table) — the violation is size, not dialect.

---

## 5. Additional observations (outside the four prompts, load-bearing enough to record)

### 5.1 `LocalDocumentStore` sync I/O in async methods — NOTE (accepted, documented)

- `local_document_store.py:91,120` use blocking `open()/read()/write()` inside `async def`,
  suppressed with `noqa: ASYNC230` and a comment reserving asyncio streaming for the Azure
  adapter. Acceptable for the local profile (FastAPI threadpool), correctly marked as a seam.
  No action; do not "fix" by adding bare `asyncio.to_thread` without measuring — just keep the
  seam note.

### 5.2 `SqlSlaClockRepository._to_row` drops columns the domain carries — MINOR

- `sla_clock_repository.py:59-89`: `escalation_on_warning`/`escalation_on_breach`/`warning_at`
  are folded into `metadata_json` because H0 columns don't exist yet. `_to_domain` (`:27-56`)
  pops them back out. Round-trip holds only because both functions agree on the stuffing keys.
  Any third reader of `metadata_json` (report, export) will see synthetic keys. Documented in
  the function comment — acceptable for H0, but the H1 migration must promote these to real
  columns rather than extending the convention.

### 5.3 Workflow instance maps to an application object — MINOR (layering)

- `workflow_instance_repository.py:12` imports `WorkflowInstance` from
  `app.application.workflow.start_workflow`, and `_domain` (`:19-33`) / `add` (`:41-55`) speak
  that type. Every sibling repository maps to a **domain** entity. The runtime instance concept
  should live in `app.domain.workflow` (or the port should be typed `Any`, as `TaskRepository`
  already is for its pre-domain phase) so `infrastructure → application` edges don't accumulate.
  No runtime bug — direction is still inward-safe — but `import-linter` layer rules should be
  checked against this edge explicitly.

### 5.4 Retention/legal-hold repositories expose sync-shaped `update` stubs — NOTE (verify)

- `retention_repository.py:183,254,310,371` and `legal_hold_repository.py:78` (in-memory) expose
  `update(...)` paths whose durability story differs (in-memory vs future-SQL). Covered by the
  FM-039 seam note; just ensure the SQL adapter's `update` enforces the same `Conflict` semantics
  the in-memory one does (`legal_hold_repository.py:86-92`, tenant-mismatch → `Conflict`).

---

## 6. Checklist against the review prompts

- [x] **Port implementations verified** — §1. Gaps: Queue surface (§1.2), Case queue listing stub
      (§1.1), DocumentStore `get` bug (§1.3), `legal_hold` mapping drop (§1.6). Drift: MessageBus
      envelope (§1.4), test-shim import (§1.8), idempotency TTL (§1.7).
- [x] **Tenant isolation in queries checked** — §2. All domain reads explicitly scoped (§2.1);
      listener is defense-in-depth only. Worker paths intentionally global; `replay()` needs a
      tenant parameter (§2.2).
- [x] **Audit logging integration validated** — §3. Chain/dedupe/reads correct (§3.1); 3 of 4
      enforcement layers solid, raw-SQL path relies on trigger alone (§3.2); JSON LIKE filter is
      brittle (§3.3).
- [x] **Migration portability reviewed** — §4.1–§4.2 pass for sqlite/postgres; Azure risks
      tracked in §4.3 (trigger dialect branch, `tuple_()` pagination, `SignedAuditBatch` drift,
      `occurred_at` typing). Models size violation in §4.4.
- [x] **Raw SQL dialect usage confirmed** — §4.2: no banned tokens in DDL/DML; only trigger DDL
      (annotated) + one unannotated portable `UPDATE` in `0015`.

## 7. Suggested triage order (for the owner, not a fix)

1. `CRITICAL` §1.3 (DocumentStore `get`) + `MAJOR` §1.6 (`legal_hold` mapping) — both are silent
   data-path breakages; verify with a round-trip test before any further slice work.
2. `MAJOR` §1.2 / §1.1 — decide whether queue surfaces are in-slice (implement) or out-of-slice
   (remove from port + router so the contract doesn't promise what the adapter can't do).
3. `MAJOR` §3.2 + §4.4 — close the audit-bypass wording and split `models.py` (R2 gate).
4. `MINOR` batch (§1.4, §1.7, §1.8, §2.1–§2.2, §3.3, §4.3–§4.4, §5) — seam backlog + hardening.

*End of review. No code was modified; this document is the only artifact.*
