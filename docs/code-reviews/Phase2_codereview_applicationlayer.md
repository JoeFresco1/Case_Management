# Phase 2 Code Review — Application Layer (`backend/app/application`)

> **Scope:** read-only review. No code was modified.
> **Date:** 2026-09-10 | **Reviewer:** AI code review (Muse Spark via OpenCode)
> **Surface:** 121 `*.py` files under `backend/app/application/` (31 sub-packages).
> **Review axes (per request):** (1) Protocol dependencies point only to domain,
> (2) idempotency implementations, (3) authorization policies,
> (4) outbox/concurrency patterns, (5) case versioning logic.
> **Method:** full `Glob` + `Grep` import scan of all 121 files, targeted `Read`
> of `authorization.py`, `cases/create_case.py`, `cases/edit_case.py`,
> `tasks/complete_task.py`, `workflow/execute_transition.py`,
> `interfaces/http/idempotency.py`, `pyproject.toml`, plus executed
> verification: `ruff check`, `mypy --strict`, `lint-imports`.

## Verification evidence (executed, not assumed)

| Check | Command | Result |
|---|---|---|
| Ruff | `python -m ruff check backend/app/application` (from `backend/.venv`) | **All checks passed** |
| mypy strict | `python -m mypy --strict app/application` (from `backend/`, config `backend/pyproject.toml:54-64`) | **Success: no issues found in 121 source files** (note: `strict=true` but `disallow_any_generics` / `disallow_any_expr` are NOT enabled — see §1.3) |
| import-linter | `lint-imports` (from `backend/`, contracts `backend/pyproject.toml:108-129`) | **3 kept, 0 broken** — "Domain layer is pure KEPT", "Application depends only on domain KEPT", "Interfaces do not import infrastructure directly KEPT" (328 files, 2006 deps analyzed) |
| File sizes (R2) | line-count over `backend/app/application/**/*.py` | **2 files > 1000 lines (R2 hard-limit violation)** — see §1.4 |

---

## 1. Protocol dependencies — do they point only to domain?

### 1.1 Verdict: PASS on hard layering, FAIL on hygiene

A regex scan for executable imports matching
`app.infrastructure | app.interfaces | app.main | app.deps | app.db | app.config | fastapi | sqlalchemy | alembic`
returned **0 hits** across all 121 files. The ~55 raw string hits for those
stems are all comments/docstrings/seam notes or string-literal adapter refs
(e.g. `webhooks/outbound.py:78-80`, `connectors/manage_connectors.py:41`),
not imports. No `importlib`/`__import__` dynamic loading and no direct
infra-class construction were found. Observed top-level imports are:
`__future__`, stdlib (`dataclasses`, `datetime`, `uuid`, `collections.abc`,
`typing`, `enum`, `hashlib`, `hmac`, `json`, `csv`, `io`, `zipfile`,
`base64`, `binascii`, `re`, `asyncio`, `logging`, `contextlib`, `abc`),
`app.domain.*`, intra-layer `app.application.*`, plus the two exceptions
below. This agrees with the `lint-imports` "Application depends only on
domain KEPT" result.

### 1.2 Finding [Major] — `structlog` imported directly in application layer

- **File:** `backend/app/application/search/semantic_search.py:62`
  (`import structlog`), used at `:87` (`_LOGGER = structlog.get_logger(__name__)`).
- **Why it matters:** only unconditional third-party top-level import in the
  layer. AGENTS.md §4.R10 requires cross-cutting concerns (telemetry) to live
  in middleware/deps or `infrastructure/telemetry/`, injected as a `Logger`
  Protocol — not imported by application use-cases. Every other file is clean
  on this axis.
- **Recommendation (not applied):** inject a `Logger` Protocol or move logging
  to an interface/infra decorator.

### 1.3 Finding [Minor, systematic] — `uuid_extensions` (PyPI `uuid7`) lazy-imported in 17 files

- **Files:** `cases/create_case.py:333`, `cases/edit_case.py:273`,
  `cases/relate_cases.py:277`, `cases/merge_cases.py:229`,
  `cases/split_case.py:382`, `documents/upload_document.py:426`,
  `documents/version_document.py:303`, `documents/protect_document.py:397`,
  `forms/submit_form.py:434`, `holds/manage_holds.py:314`,
  `retention/archival.py:155`, `retention/disposition.py:676`,
  `sla/scan_sla.py:459`, `tasks/complete_task.py:292`,
  `workflow/execute_transition.py:1112`, `workflow/start_workflow.py:331`,
  `audit/siem_stream.py:861` (most defensive — `try/except ImportError` with
  `uuid4` fallback).
- **Why it matters:** strictly off the allowlist (not stdlib / `pydantic` /
  `typing` / `domain`). Low risk (function-level, documented), but 17 copies
  of the same fallback idiom is a port waiting to happen. `pyproject.toml:70-72`
  already special-cases `uuid_extensions.*` in mypy overrides.
- **Recommendation (not applied):** centralize behind `app.domain.ports`
  (`UuidFactory`) or `Clock`, or a single `application/_ids.py` helper.

### 1.4 Finding [Major] — R2 hard-limit breach: 2 files > 1000 lines

AGENTS.md §4.R2: "no source file >1000 lines" is a hard limit.

| File | Lines |
|---|---|
| `backend/app/application/workflow/execute_transition.py` | **1209** |
| `backend/app/application/webhooks/outbound.py` | **1135** |
| (near-limit, for watchlist) `reports/custom_reports.py` 919, `imports/run_import.py` 911, `retention/archival.py` 894, `audit/siem_stream.py` 884, `queues/manage_queues.py` 880 | — |

Both over-limit files are also complexity hotspots flagged elsewhere in this
report (`execute_transition.py` carries the SoD bypass at `:1123-1139`, the
swallowed-validation path at `:1104-1108`, and the `_actor_proxy` at `:548`).

### 1.5 Finding [Moderate, pervasive] — `Any` erases injected ports (passes mypy only because strict is incomplete)

`mypy --strict` passes, but the config does not enable `disallow_any_generics`
or `disallow_any_expr`, so `dict[str, Any]` payloads and `clock: Any`-style
port erasure are invisible to the gate. AGENTS.md §4.R6 says "no `Any` outside
ports". Observed:

- **CLEAN (no `Any`):** `authorization.py`, `ai/decision.py`,
  `search/semantic_search.py`, `search/case_search.py`,
  `reports/operational_reports.py`, `reports/dashboards.py`, all
  `*/http_dependencies.py` (which correctly use `Protocol`).
- **ALLOWED (inside `Protocol` body):** `forms/submit_form.py:105`,
  `forms/advanced_execution.py:223`, `queues/manage_queues.py:101-154`,
  `tasks/complete_task.py:79-102`, `imports/http_dependencies.py:40`,
  `audit/http_dependencies.py:13-19`.
- **MEDIUM — port-typed parameters declared as `Any`:**
  `audit/correct_audit.py:143-144` (`dedupe: Any, clock: Any`),
  `audit/export_audit.py:242-243`, `capture/email_capture.py:141`
  (`clock: Any`), `capture/webhook_commands.py:144+`,
  `capture/scan_capture.py:216`, `channels/manage_channels.py:237+`,
  `connectors/manage_connectors.py:222-225`
  (`connector_repo: Any, audit: Any, outbox: Any, clock: Any` — heaviest),
  `documents/upload_document.py:136-141`, `documents/version_document.py:107+`,
  `documents/download_document.py:48`, `documents/protect_document.py:113`,
  `exports/evidence_bundle.py:423-424`, `forms/advanced_execution.py:267-269`,
  `imports/run_import.py:264`, `migration/coexistence.py:113-115`,
  `reports/custom_reports.py:490,772-773`, `webhooks/outbound.py:1024`
  (`delivery_service_factory: Any`), plus the `_user_context() -> Any` /
  `_actor_proxy() -> Any` helpers in `create_case.py:297`,
  `upload_document.py:389`, `execute_transition.py:548`,
  `complete_task.py:282`, `advanced_execution.py:711`, etc.
- **LOW (acceptable JSON-envelope `dict[str, Any]`):** `ai/boundary.py:47`,
  `ai/search_ranking.py:49`, `audit/record_event.py:38`,
  `cases/*` data payloads, `compute_derived.py`, `redactor.py`,
  `summary.py:158`, `view_case.py`, `workflow/*` simulation/validation JSON.
- **Why it matters:** `clock: Any` / `audit: Any` / `repo: Any` defeats the
  Protocol-dependency rule the review was asked to verify — a caller can pass
  anything and mypy stays green. Exemplars that do it right
  (`holds/`, `retention/`, `participants/` use real `domain.ports.*` types)
  prove the pattern exists; it is just not applied uniformly.
- **Recommendation (not applied):** replace with `app.domain.context.Clock` /
  `UserContext` and `domain.ports.*` Protocols; alias JSON envelopes as
  `JsonPayload = dict[str, Any]` in domain if `disallow_any_generics` is ever
  enabled.

---

## 2. Idempotency implementations

### 2.1 Finding [Critical] — `PATCH` mutations have zero idempotency enforcement

- **Files:** `backend/app/interfaces/http/idempotency.py:82-92` —
  only `POST` without a key is rejected (`400`); `PATCH/PUT/DELETE` without a
  key falls through to `call_next` (`:92`). `PATCH /cases/{id}`
  (`routers/cases.py:411-420`) enforces only `If-Match`, never
  `Idempotency-Key`.
- **Why it matters:** directly contradicts AGENTS.md §4.R7/§10 ("idempotency
  on mutations"). Any `PATCH` mutation executes with zero replay protection
  unless the caller happens to send a key. `edit_case.py:32-37` even documents
  that "a second call with the same body and a current token still produces a
  new audit row", i.e. the gap is known and pushed to the client.
- **Related inconsistency [Major]:** router-level enforcement is uneven —
  `routers/cases.py:182-191` (`POST /cases`),
  `routers/tasks.py:103-107`, `routers/documents.py:180+`,
  `routers/capture.py:188,328`, `routers/forms.py:123` explicitly `400` on a
  missing key (double-enforcement with the middleware), while
  `routers/cases.py:596-604` (`POST /cases/{id}/transitions`) has **no** router
  check and docstring `:616` says callers "should always carry" a key (i.e.
  optional). `execute_transition.py:32-39` admits "the use case itself is
  **not** idempotent at the domain layer".

### 2.2 Finding [Major] — idempotency key stored in plaintext

- **Files:** `infrastructure/persistence/models.py:423` (`key: String(128)`
  PK), `infrastructure/persistence/idempotency_http_dependencies.py:111-119`
  (inserts raw `key`). Only the *body* is hashed
  (`idempotency.py:45-56` canonical JSON `sort_keys`, `method+path+body` →
  SHA-256; `:116-129` wraps as `operation_hash + actor + sorted roles +
  system` → final `request_hash`; mismatch → `409` at `:141-144`).
- **Why it matters:** a DB leak exposes live replay keys. The spec question
  "are keys hashed" fails for the key column itself.

### 2.3 Finding [Major] — `expires_at` column exists but is never set or enforced

- **Files:** `models.py:429-432` (explicit comment "middleware does not
  enforce expiry yet"); provider insert
  (`idempotency_http_dependencies.py:111-119`) sets `created_at` only, leaves
  `expires_at=None`; no read path (`_reserve`, `get`, `put`) checks expiry.
- **Why it matters:** keys live forever → unbounded `idempotency_keys` growth,
  no TTL/GC, permanent `409` on key reuse.

### 2.4 Finding [Major] — in-flight duplicate gets `503`, not wait/serialize (doc mismatch)

- **Files:** `idempotency_http_dependencies.py:62-66` docstring claims
  "Concurrent callers wait on its unique constraint", but `:100-146` `_reserve`
  catches `IntegrityError`, re-`SELECT`s, and if `prior is None or
  response_status==0` raises `DependencyUnavailable` (`:136-137` → middleware
  `503` at `idempotency.py:108-109`). No blocking/polling.
- **Why it matters:** correctness is OK (second caller never enters the
  handler — no double-execute) but availability + documentation mismatch;
  clients must retry on `503` and nothing tells them so.

### 2.5 Finding [Major] — legacy `SqlIdempotencyStore.put` is check-then-insert without `IntegrityError` handling

- **File:** `infrastructure/persistence/audit_outbox.py:508-543`.
  Concurrent `put` for the same `(tenant_id, key)` can raise an unhandled
  `IntegrityError` instead of a clean `409`/`503`. Hash mismatch raises a bare
  `ValueError` (`:531`) with no documented HTTP mapping (vs. the middleware
  path's `409`).

### 2.6 Finding [Major] — multipart uploads break legitimate retry (false `409`)

- **Files:** `idempotency.py:49-50` — non-JSON bodies fall back to
  `{"raw_sha256": sha256(body)}`. `upload_document` is multipart; boundary
  bytes change per attempt, so an identical file + same key hashes differently
  → false `409 Idempotency-Key belongs to another request`.
- **Related [Minor]:** canonicalisation quirks — query always appended as
  `path+"?"+query` (`:117`); role change intentionally invalidates retry
  (`:119,122-124` `sorted(user.roles)`) so a retry after a grant/revocation
  gets `409` instead of replay (by design, but operationally surprising).

### 2.7 What is correct (for balance)

- Tenant scoping: PK `(tenant_id, key)` (`models.py:420-423`); every lookup
  filters both; reservation tenant comes from authenticated `user.tenant_id`
  (`idempotency.py:134-138`); `x-dev-tenant` mismatch → `401` (`:204-207`).
- Replay returns the stored response (`:140-145` `_replay`, `:175-189`
  restores status + base64 body, sets `idempotency-replay: true`).
  Only `2xx` finalised (`:153`); errors roll back so never cached.
- Cross-operation reuse correctly rejected: different op/actor → `409`, so a
  key cannot disclose another actor's response (`:119` comment).
- Length guard `>128` → `400` matches `String(128)` column.
- Fragile but present: `finalize` refuses rolled-back tx; route handlers also
  call `db.commit()` directly (`routers/tasks.py:162`,
  `routers/cases.py:641,480`) so atomicity depends on
  `join_transaction_mode="rollback_only"` wiring — split-session shape is not
  obvious from router code.

---

## 3. Authorization policies

Core policy (`application/authorization.py:409-436`) **is** deny-by-default:
unknown `Action` denied (`:409-410`), cross-tenant denied (`:414-415`), static
roles checked (`:422-424`), then live assignments (`:430-433`), else deny
(`:436`); `authorize()` raises `Forbidden` (`:503-504`). Unknown role names
ignored (`:351,379-382`). The canonical four use-cases all call `authorize`
(`create_case.py:145` `CASE_CREATE`, `edit_case.py:139` `CASE_UPDATE`,
`execute_transition.py:291` `CASE_TRANSITION`, `upload_document.py:157`
`DOCUMENT_UPLOAD`, `version_document.py:129`, `complete_task.py:188`
`TASK_COMPLETE`; ~98 `authorize(` hits across the layer).

### 3.1 Finding [Major] — six mutating `handle()`s have no in-use-case `authorize`; enforcement is router-only

- **Files:** `configuration/draft_case_type.py:92`,
  `configuration/publish_case_type.py:114`,
  `workflow/create_workflow.py:56`, `workflow/draft_workflow.py:100`,
  `workflow/publish_workflow.py:112`, `workflow/start_workflow.py:148`.
- **Why it matters:** sole enforcement is the router
  (`routers/admin_case_types.py:106,176,216` → `ADMIN_TENANT_CONFIG`;
  `routers/admin_workflows.py:138` → `WORKFLOW_PUBLISH`). Any non-HTTP caller
  (CLI, background job, test, future router) bypasses auth entirely. Violates
  AGENTS.md §4.R12 ("every protected path must be policy-evaluated",
  server-side enforcement). `start_workflow` is currently internal-only (called
  from `create_case.py:260`, inheriting `CASE_CREATE`) but has no independent
  check if ever invoked directly.

### 3.2 Finding [Major] — `SYSTEM_BYPASS` is empty (dead path) while a parallel string-based SoD bypass exists

- **Files:** `authorization.py:170` (`SYSTEM_BYPASS = frozenset()`) +
  `:418-419` system branch; all use-case proxies hardcode `is_system=False`
  (`create_case.py:322-326`, `complete_task.py:271-279`,
  `execute_transition.py:537-545`, `documents/upload_document.py:403-416`), so
  the documented system path is unreachable (fail-closed, but contradicts the
  module docstring `:28-31,401-402`). Separately,
  `workflow/execute_transition.py:1123-1139,1173-1177` implements a **parallel
  SoD bypass** on the role-string `"system"` (`_is_system`, `bypass_system`),
  not via `Policy`/`SYSTEM_BYPASS` — SoD can be skipped by anyone holding a
  role literally named `system`.
- **Why it matters:** two authorization mechanisms for one concept; the live
  one is a string comparison outside the policy, unaudited by
  `role_actions()`.

### 3.3 Finding [Minor] — authorize-before vs. resolve-before order is inconsistent

- `create_case.py:145` + `upload_document.py:157` authorize before any DB read;
  `complete_task.py:176-192` (verified: `get` at `:176`, `case_lookup` at
  `:182`, `authorize` at `:188`) and `execute_transition.py:285-295` do a
  tenant-scoped `get()` first, then `authorize`. The latter performs a DB read
  for an unauthorised actor (timing/oracle surface; both map cross-tenant to
  `NotFound`, so exposure is limited).

### 3.4 Finding [Minor] — docstring/code mismatch on cross-tenant error shape

- `upload_document.py:8,164-166` header says a cross-tenant case "surfaced as
  `Forbidden`", code raises `NotFound`. `complete_task.py:171-184` /
  `execute_transition.py:282-287` intentionally use `NotFound` for both cases.
  Inconsistency risks leaking existence assumptions in the one place that
  documents `Forbidden`.

### 3.5 Finding [Minor] — decision audit is opt-in

- `authorization.py:462-504` — `access_recorder=None` by default. Auth is still
  enforced, but allow/deny produces no `AccessDecisionRecorded` audit row
  unless the router passes a recorder. P09-US6/P11-US9 audit-of-decisions is
  easy to omit silently.

---

## 4. Outbox / concurrency patterns

### 4.1 What is correct (verified)

Canonical mutations stage `repo` + `audit.append` + `outbox.enqueue` with the
caller committing once (all docstrings state the contract explicitly):

| Use-case | Evidence |
|---|---|
| `cases/create_case.py` | `:215,217,239` + workflow start joins same txn `:259-276` |
| `cases/edit_case.py` | `:194,196,215` ("share one transaction" `:123-132`) |
| `workflow/execute_transition.py` | `:402,421,445,485,505` (`bump_state` + `case_repo.update` + `task_repo.add` + `sla_clock_repo.add` + audit + outbox + `transition_log.record`; `:264-268`) |
| `tasks/complete_task.py` | `:210,214,234,253` + optional `transition_runner` in same txn (`:160-165`; router shares session `routers/tasks.py:122-143`) |
| `documents/upload_document.py` / `version_document.py` | `:244,246,271` / `:214,216,242` ("same transaction" `:30-34`) |
| `participants/manage_participants.py` | `:90,132,170,198-231` ("caller's transaction" `:210`) |
| Infra | `audit_outbox.py:447-476` `SqlOutboxWriter.enqueue` is `session.add` only (no direct `MessageBus.publish` — correct); commit at boundary (`routers/cases.py:221,480,641`, `tasks.py:162`, `documents.py:131,238,287,297,432,639,708`); `outbox_dispatcher.py:161-245` `drain_once` + `MAX_ATTEMPTS=51`, backoff `:64-69`, unknown-type dead-letter `:200-210`, `replay:319-344` — sane |

### 4.2 Finding [Moderate] — `publish_*` writes audit but no outbox

- **Files:** `configuration/publish_case_type.py:148-178`
  (`mark_status` + `set_current_version` + `audit_store.append(ConfigurationPublished)`
  only); `workflow/publish_workflow.py:112-197` (same shape).
- **Why it matters:** breaks the "every mutation → audit+outbox" invariant
  claimed at `ports.py:1488-1491,1651-1654` and
  `execute_transition.py:51-52` ("immutable audit+outbox pair"). Config
  publishes are invisible to subscribers/dispatcher.

### 4.3 Finding [Moderate] — `DocumentStore.put` happens outside the DB transaction (orphan bytes)

- **Files:** `documents/upload_document.py:206-211` (`await
  document_store.put(...)` *before* `add_document_and_version:244` +
  audit/outbox); `version_document.py:179-184` before `:214-256`;
  `protect_document.py` same pattern.
- **Why it matters:** failure doc (`:202-205`) says "caller rolls back; no
  audit row" — true for the DB, but a successful `put` + failed `commit`
  leaves content-addressed bytes with no metadata row. No GC / compensating
  delete documented.

### 4.4 Finding [Moderate] — outbox envelope mismatch: `correlation/causation/occurred_at` lost on the wire

- **Files:** dispatcher expects `__correlation_id/__causation_id/__occurred_at`
  inside `payload` (`outbox_dispatcher.py:81-96` `_envelope`, `:98-119`
  `_rebuild_payload`). No caller sets them: `create_case.py:239-251`,
  `edit_case.py:215-229`, `execute_transition.py:485-500`,
  `complete_task.py:234-246`, `upload_document.py:271-285`,
  `manage_participants.py:229-231` all enqueue without envelope keys.
- **Why it matters:** `_envelope` returns `(None,None,None)` (`:213`),
  `bus.publish(..., correlation_id=None ...)` (`:217-225`). Breaks the
  FM-055 "subscriber receives correlation_id" claim at `:24-26`.

### 4.5 Finding [High] — `next_reference` check-then-act race (duplicate `reference` under concurrency)

- **Files:** `infrastructure/persistence/repositories/case_repository.py:107-138`:
  `SELECT reference ... ORDER BY DESC LIMIT 1` then `seq+1` in Python, no
  `SELECT ... FOR UPDATE` / sequence / advisory lock. Two concurrent
  `create_case.py:167` calls compute the same `seq`.
- **Why it matters:** only safety is the unique index
  `(tenant_id, reference)` → second `commit` raises `IntegrityError` (500),
  not a clean `Conflict`/retry. Monotonic-reference invariant
  (`case.py:42`, `ports.py:37`) holds in the serial case only.

### 4.6 Finding [Moderate] — `SqlDocumentRepository.add_version` blind overwrite (no CAS)

- **File:** `infrastructure/persistence/documents.py:239-253` — `UPDATE
  DocumentRow WHERE id+tenant_id` with no `current_version == expected`
  predicate. Two concurrent `version_document.py:190-214` both read
  `current_version=1`, both `with_new_version → 2`, both `add(version_number=2)`
  → duplicate row + last-writer-wins pointer. Contrast
  `SqlCaseRepository.update:62-76`, `SqlTaskRepository.update:34-45`,
  `SqlWorkflowInstanceRepository.bump_state:103-117`, which all CAS.
- Same file `:282-295 save_locked`, `:309-322 add_redaction` — no token guard
  (acceptable for append-only redaction, but the lock itself is racy).

### 4.7 Finding [Low] — `execute_transition._apply_set_field_actions` silently swallows validation

- **File:** `workflow/execute_transition.py:1104-1108` —
  `except (ValidationFailed, InvariantViolation): return case, {}`. The state
  move (`bump_state:402` + `case_repo.update:421`) still commits while
  `set_field` writes vanish; audit `fields_written:462` is `{}` but the caller
  gets success. Should be `GuardBlocked`/rollback.

### 4.8 Finding [Low] — upload duplicate check is TOCTOU

- **File:** `upload_document.py:194-200` — `list_for_case` scan for `sha256`
  then `put`+`add`. No `UNIQUE(tenant_id, case_id, sha256)` guard; concurrent
  identical uploads both pass the check and both insert.

### 4.9 Concurrency — `PATCH /cases/{id}` is the exemplar; everything else is missing the guard

**PASS (verified):** router requires `If-Match`
(`routers/cases.py:418,425-436` → 400 if missing/malformed);
`EditCaseCommand.expected_version_token:90` plumbed (`:446`); domain
double-check (`domain/case.py:267` `assert_concurrency_token`, `:399-401`
`Conflict`, bump `:311-316`, no-op returns `self` `:304-309` with caller
skipping audit `edit_case.py:176-177`); SQL CAS
(`case_repository.py:62-76`); 409 enrichment with current token
(`routers/cases.py:459-479`, `ETag` `:520`). `version_token` increment rules
correct: create `=1` (`case.py:218`), edit `+1` (`:315`), `change_status +1`
(`:365`), `set_workflow_state +1` except initial (`:369-397`, keeps `1` so
`create_case.py:210` doesn't ship `2`).

**FAIL — mutations missing concurrency guards:**

- **[High] `POST /cases/{id}/transitions` — no `If-Match` /
  `expected_version_token`.** Command has no token
  (`execute_transition.py:139-159`); router takes no header
  (`routers/cases.py:596-640` vs `PATCH :418`). Protection is only
  server-side `bump_state(expected=instance.version_token:402-408)` +
  `case_repo.update` CAS (`:421`). A client cannot express "I saw state X /
  token N"; two racers both pass the guard on a stale read and the loser gets
  `Conflict` from `workflow_instance_repository.py:103-117` mapped as generic
  409, not "stale token + current token" like PATCH. Violates AGENTS.md
  §10/§17 ("`If-Match` required on mutations").
- **[High] `POST /tasks/{id}/complete` — no `If-Match` /
  `expected_version_token`, check-then-act race.** Command has no token
  (`complete_task.py:105-129`); router enforces only `Idempotency-Key`
  (`routers/tasks.py:97,103-107`). Flow `complete_task.py:176-210` is
  `get → complete() → if token changed: update`; `Task.complete`
  (`domain/task.py:176-215`) bumps without checking a caller token. SQL CAS
  exists (`task_repository.py:29-45`) but without a client anchor, concurrent
  duplicates raise `Conflict` instead of the idempotent no-op the docstring
  (`:166-169,194-208`) promises. Transport replay relies solely on the
  middleware, not the domain token.
- **[Moderate] Participants add/update/remove — no `version_token` at all.**
  Commands (`manage_participants.py:26-65`) carry no token; domain
  `Participant.update/remove` takes no `expected_version_token`;
  `ParticipantRepository.update` (`ports.py:2054-2055`) is blind. Concurrent
  updates last-writer-wins yet both emit audit+outbox.
- **[Moderate] Document version/redact/lock — no `version_token`.**
  `VersionDocumentCommand:73-87`, `RedactDocumentCommand:71-91` carry no token;
  `Document.with_new_version` bumps `current_version` with no
  `assert_concurrency_token` (see §4.6 blind update).
- **[Moderate] `Case.change_status` / `set_workflow_state` take no
  `expected_version_token`.** (`domain/case.py:338-367`, `:369-397` — both
  bump but accept no expected token, unlike `edit:229-239`.) Any internal
  caller (workflow engine `:410`, SLA scanner, admin close) can move state
  without a freshness check; only the SQL CAS at `update` time stands guard,
  and callers don't surface it as 409-with-current-token.

---

## 5. Case versioning logic

### 5.1 What is correct (verified)

- Domain immutability: `domain/case_type.py:418-437` publish
  (`draft→published` else `InvariantViolation`), `:439-460` retire,
  `:464-473` `assert_mutable`; `domain/workflow.py:436-457` publish,
  `:459-483` retire, `:487-498` `assert_mutable` — identical shapes.
- Running cases pinned: `execute_transition.py:305-310` loads
  `instance.workflow_version_id`, rejects `RETIRED` (`:309`);
  `edit_case.py:149-160` validates against pinned `case_type_version_id`,
  refuses cross-version writes.

### 5.2 Finding [Moderate] — `set_current_version` has no CAS (concurrent publishes overwrite)

- **Files:** `domain/ports.py:453-463`
  (`WorkflowDefinitionRepository.set_current_version(tenant, def_id,
  version_id)` — docstring claims "MUST compare-and-swap on existing
  `current_version_id`" but the signature carries no `expected_version_id`).
  Same for `CaseTypeRepo.set_current_version`
  (`publish_case_type.py:50-54` local Protocol). Two concurrent
  `publish_case_type.py:156` / `publish_workflow.py` equivalents both
  `mark_status → published`, then last `set_current_version` wins — no
  `Conflict`.

### 5.3 Finding [Low] — DB-level immutability deferred by design (domain-only today)

- **Files:** `case_type.py:467-470`, `workflow.py:491-493` ("Database-level
  immutability is added in a later phase via trigger+ORM listener").
  `mark_status` must reject non-`draft→published`/`published→retired`
  (`ports.py:509-525`). Confirm the trigger/listener exists before claiming
  AC-008; today a direct SQL `UPDATE` bypasses the domain guard.

### 5.4 Finding [Info] — stale-token → `409` mapping verified only for PATCH

- `domain/errors.py:29 Conflict`; `case.py:399-401`;
  `case_repository.py:76`, `task_repository.py:45`,
  `workflow_instance_repository.py:92,117` all raise `Conflict`. Router maps it
  for PATCH (`cases.py:459-479`) and forms (`forms.py:99,430`). Transition/task
  CAS `Conflict` has no dedicated `try/except` in
  `routers/cases.py:619-641` / `routers/tasks.py:145-162` — surfaces via the
  global handler (verify `interfaces/http/errors.py` maps `Conflict → 409`
  with `code=conflict`; unlike PATCH there is no `X-Current-Version-Token`
  enrichment).

### 5.5 Finding [Low, contract drift] — `ports.py` header promise "every repository method takes `tenant_id` first" is not true for writes

- **File:** `backend/app/domain/ports.py:7-29` (claim) vs. `add`/`update`
  signatures throughout, which take the tenant from the aggregate instead:
  `CaseRepository.add/update:102-105` (reads OK `:117-138`);
  `CaseTypeRepository.add:159-161`; `CaseTypeVersionRepository.add:167-176`;
  `DocumentRepository.add_document_and_version / add_version / save_locked /
  add_redaction:191-262`; `QueueRepository.add/update:569-582` (also untyped
  `Any`); `TaskRepository.add/update:615-625`;
  `WorkflowInstanceRepository.add/update:716-737` (`bump_state:739-762`
  correctly takes `tenant_id` + `expected_version_token`);
  `WorkflowTransitionLog.record:774`; `SlaClockRepository.add/update:809-837`;
  `RoleRepository.add:1171-1192`; `SessionRepository.add/save:1239-1250`;
  `ChannelRepository:1320-1322`, `QuarantineRepository:1348-1350`,
  `SavedSearchRepository:1648`, etc.
- **Security impact: Low** — SQL impls do include `WHERE tenant_id`
  (`case_repository.py:65,79,84,100`, `task_repository.py:37`,
  `workflow_instance_repository.py:81,106`,
  `audit_outbox.py:123,217,275,413,494`). `OutboxWriter.enqueue:1044-1051`,
  `AuditEventStore.append:926-940`, `IdempotencyStore:1067-1080`,
  `DocumentStore:1091-1099` are correctly explicit — keep those as exemplars.
  `TenantScoped:80-87` is a marker with no runtime/static enforcement, so the
  header promise is unenforceable.
- **Recommendation (not applied):** fix the docs to "reads take `tenant_id`;
  writes take it from the aggregate + enforce `WHERE tenant_id`", or add
  `tenant_id` to `add`/`update` signatures.

---

## 6. Consolidated issue list (severity-ordered)

| # | Severity | Area | Location | Issue |
|---|---|---|---|---|
| 1 | Critical | Idempotency | `interfaces/http/idempotency.py:82-92`, `routers/cases.py:411-420` | `PATCH/PUT/DELETE` without key bypass middleware; `PATCH /cases/{id}` has no idempotency |
| 2 | High | Concurrency | `routers/cases.py:596-640`, `application/workflow/execute_transition.py:139-159` | Transitions have no `If-Match`/`expected_version_token`; client cannot express freshness |
| 3 | High | Concurrency | `application/tasks/complete_task.py:105-129`, `routers/tasks.py:97-107` | Task-complete has no `If-Match`; check-then-act race; CAS `Conflict` instead of idempotent no-op |
| 4 | High | Outbox/refs | `infrastructure/persistence/repositories/case_repository.py:107-138` | `next_reference` check-then-act race; dup `reference` → raw `IntegrityError` (500) |
| 5 | Major | Layering | `application/search/semantic_search.py:62,87` | Direct `structlog` import in application layer |
| 6 | Major | Layering (R2) | `application/workflow/execute_transition.py` (1209 lines), `application/webhooks/outbound.py` (1135 lines) | Hard-limit >1000 lines breach |
| 7 | Major | Authz | 6 `handle()`s (config `draft/publish_case_type`, workflow `create/draft/publish/start`) | No in-use-case `authorize`; router-only enforcement bypassable by non-HTTP callers |
| 8 | Major | Authz | `authorization.py:170` + `workflow/execute_transition.py:1123-1139` | Empty `SYSTEM_BYPASS` (dead path) + parallel string-based `"system"` SoD bypass |
| 9 | Major | Idempotency | `infrastructure/persistence/models.py:423`, `idempotency_http_dependencies.py:111-119` | Key stored plaintext; only body hashed |
| 10 | Major | Idempotency | `models.py:429-432`, `idempotency_http_dependencies.py:111-119` | `expires_at` never set/enforced; unbounded growth |
| 11 | Major | Idempotency | `idempotency_http_dependencies.py:62-66,100-146` | In-flight dup → `503`, contradicts "wait" docstring |
| 12 | Major | Idempotency | `infrastructure/persistence/audit_outbox.py:508-543` | Legacy `put` check-then-insert, no `IntegrityError` handling; bare `ValueError` on hash mismatch |
| 13 | Major | Idempotency | `interfaces/http/idempotency.py:49-50` + `documents/upload_document.py` | Multipart retry hashes boundary bytes → false `409` |
| 14 | Moderate | Typing | ~30 files (`connectors/manage_connectors.py:222-225` worst; `audit/*`, `capture/*`, `channels/*`, `documents/*`, `imports/*`, `migration/*`, `reports/*`, `_actor_proxy/_user_context -> Any`) | `Any`-erased ports defeat Protocol checking; mypy gate blind to it |
| 15 | Moderate | Outbox | `configuration/publish_case_type.py:148-178`, `workflow/publish_workflow.py:112-197` | Publish writes audit but no outbox; subscribers blind |
| 16 | Moderate | Outbox | `documents/upload_document.py:206-211`, `version_document.py:179-184` | `DocumentStore.put` outside DB txn; orphan bytes on commit failure |
| 17 | Moderate | Outbox | `infrastructure/messaging/outbox_dispatcher.py:81-119` vs all `enqueue` callers | Envelope keys never set; `correlation_id=None` on the wire (FM-055 break) |
| 18 | Moderate | Concurrency | `infrastructure/persistence/documents.py:239-253` | `add_version` blind overwrite; duplicate `version_number` possible |
| 19 | Moderate | Concurrency | `application/participants/manage_participants.py:26-65` | No `version_token` on participants; last-writer-wins with audit |
| 20 | Moderate | Concurrency | `documents/version_document`, `protect_document` commands | No `version_token`; see #18 |
| 21 | Moderate | Concurrency | `domain/case.py:338-397` | `change_status`/`set_workflow_state` take no expected token |
| 22 | Moderate | Versioning | `domain/ports.py:453-463`, `publish_case_type.py:50-54` | `set_current_version` claims CAS, signature has no expected version; concurrent publishes overwrite |
| 23 | Minor | Deps | 17 files (`create_case.py:333` et al.) | `uuid_extensions` lazy imports; centralize behind port/helper |
| 24 | Minor | Authz | `complete_task.py:176-192`, `execute_transition.py:285-295` vs `create_case.py:145` | Resolve-before-authorize DB read for unauthorised actor |
| 25 | Minor | Authz | `documents/upload_document.py:8,164-166` | Docstring says `Forbidden`, code raises `NotFound` |
| 26 | Minor | Authz | `authorization.py:462-504` | Decision audit opt-in (`access_recorder=None` default) |
| 27 | Minor | Idempotency | Router double- vs no-enforcement; `>128` length; role-change invalidates retry | Inconsistent enforcement surface; surprising `409`s |
| 28 | Low | Outbox | `workflow/execute_transition.py:1104-1108` | `set_field` validation swallowed; state moves, fields vanish, caller sees success |
| 29 | Low | Outbox | `documents/upload_document.py:194-200` | Duplicate-sha256 check is TOCTOU; no unique guard |
| 30 | Low | Versioning | `domain/case_type.py:467-470`, `domain/workflow.py:491-493` | DB-level immutability deferred; domain-only guard today |
| 31 | Low | Versioning | `domain/ports.py:7-29` header vs `add/update` sigs | `tenant_id` promise unenforceable; fix docs or signatures |
| 32 | Info | Versioning | `routers/cases.py:619-641`, `routers/tasks.py:145-162` | CAS `Conflict` surfaces via global handler without current-token enrichment (PATCH-only today) |

---

## 7. Notes for the fixer (out of scope for this review — no code changed)

1. Highest leverage first: #1 (PATCH idempotency), #2/#3 (transition/task
   `If-Match`), #4 (`next_reference` serialization), #7 (authz in use-cases),
   #8 (kill the string-based SoD bypass or route it through `Policy`).
2. Cheapest wins: #10 (`expires_at` + sweeper), #9 (hash the key column or
   document the risk decision), #15 (enqueue on publish), #17 (set envelope
   keys at `enqueue` call sites), #23 (single `_ids.py`), #31 (one-sentence doc
   fix in `ports.py`).
3. Structural: split #6 (`execute_transition.py`, `outbound.py`) per R2 before
   they attract more logic; replace MEDIUM `Any`s (#14) with
   `domain.ports.*`/`domain.context.*` types starting with the six auth-less
   `handle()`s and the connector/migration ports.
4. Gate hardening: consider `disallow_any_generics = true` (or at minimum a
   `Grep` CI check for `clock: Any` / `-> Any` in `app/application`) so #14
   cannot regress silently; add a contract test asserting every mutating
   `handle()` calls `authorize` so #7 cannot regress.

*End of Phase 2 application-layer review. No source files were modified; this
document is the only artifact.*
