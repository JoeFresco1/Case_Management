# ADR-024 — P01 External-Ref Uniqueness Scope (Revised per Architecture Review 2026-08-31)

**Status:** Proposed — revised per `needs_revision` on gate `gate_12403aea296b` (`task_bd788cd29a47` `T02-005` at 2026-08-31 16:02:24) — awaiting re-approval  
**Date:** 2026-08-31  
**Revised:** 2026-08-31 (supersedes original Proposed 2026-08-31; original gate approved prematurely, now `needs_revision`)  
**Owner:** Platform team  
**Related Pillars:** P01 Case & Business Object Management, P11 Security & Identity, P14 Integrations & Migration  
**Pillar Question:** `specs/pillars/P01-case-business-object-management/pillar.md:8` Q3  
**Tasks/Features Affected:** FM-005 `T02-005` `task_bd788cd29a47`, FM-008 `T02-008` `task_6acf6097cdcb`, FM-010 `T04-010` `task_5691a30846b4`, FM-026 `T03-026` `task_79dbd4387ee6`, `backend/app/domain/case.py:1`, `backend/app/infrastructure/persistence/models.py`, `backend/app/application/cases/create_case.py:1`, `AGENTS.md:247`

## Context
Cases carry internal stable `id` UUID and human reference `C-{TENANT}-{YYYY}-{SEQ:06d}` `AGENTS.md:303` via `CaseRepository.next_reference` `domain/ports.py:37`. `external_ref` is a distinct optional business identifier supplied by the caller at `POST /api/v1/cases` or via import `T03-026` `task_79dbd4387ee6` (e.g., legacy OnBase ID, upstream ServiceRequest number, Panzura object id). Unlike `reference`, `external_ref` originates outside the platform and is used for cross-system correlation and idempotent import deduplication (`01-deduplicated-feature-map.md:75` FM-026). No uniqueness is enforced today; `T02-005` validates `schema_json` `AGENTS.md:272` but not cross-case `external_ref`. Prior ADR-024 recommended "no DB unique, rule-guard when needed" (Option A) — **rejected as insufficient due to concurrency race** (`needs_revision`).

Existing constraints: Tenant isolation `T01-002` `WHERE tenant_id=:tenant_id` `AGENTS.md:67`; single-DB `ADR-021` `tenant_id` everywhere; cases DDL `AGENTS.md:247` has `case_type_version_id` but no `case_type_id` denormalized nor `source_system`.

## Decision Required
The ADR must explicitly determine:
1. What `external_ref` identifies
2. Its uniqueness namespace
3. Atomic database enforcement strategy
4. NULL behavior
5. Conflict/idempotency behavior
6. Import implications
7. Migration implications if namespace changes later

## What `external_ref` Identifies
`external_ref` (column `cases.external_ref Text NULL`) is **the business identifier assigned by an upstream/import system to a case**, provided at creation time via `POST /api/v1/cases {external_ref}` or bulk import `T03-026`. It is **not** the internal `id` nor the human `reference`; it is correlation state for `P14` coexistence `T05-059` and for `FM-026` dry-run reconciliation. Stable key: `external_ref` string trimmed, case-sensitive, length ≤ 256 (enforced by `P05` schema), optional (`NULL` means "no upstream id").

## Uniqueness Namespace — Evaluation of A–D

### Option A — `UNIQUE (tenant_id, external_ref)`
- Namespace: one external id per tenant across all case types.
- **Pros:** Simple; catches cross-type duplicate if same upstream system reuses ids across types *within same tenant* unexpectedly.
- **Cons:** Overly broad if upstream reuses same numeric sequence per type (common: ServiceRequest `SR-123` and Incident `INC-123` both `123` per tenant would collide); blocks legitimate multi-type reuse.

### Option B — `UNIQUE (tenant_id, case_type_id, external_ref)` 
- Namespace: per tenant + per logical case type `case_types.id` (not per version) + external id.
- **Pros:** Matches P01 intent "`no two open cases of type ServiceRequest with same external_ref`" `P01/pillar.md:8`; allows same `123` in two types; tenant-isolated; no source column needed for v1; database-enforced composite.
- **Cons:** If two upstream systems both assign `123` for same type+tenant, they would still collide (ambiguity).

### Option C — `UNIQUE (tenant_id, source_system, case_type_id, external_ref)` or `UNIQUE (tenant_id, source_system, external_ref)`
- Namespace: per tenant + per source system + (optionally type) + external id. Requires `cases.source_system Text NULL` FK to approved connectors `T05-058` `task_051ab09b73cc`.
- **Pros:** Correct if `external_ref` is **source-assigned** and multiple sources feed same tenant/type (`P14` `FM-058`); imported `T03-026` rows carry `source_system` provenance; no cross-source collision; idempotent per source `T05-059`.
- **Cons:** Requires schema addition `source_system` column + seeding of approved source registry `T05-058`; import path must populate it; v1 cases table has no such column — migration needed.

### Option D — No uniqueness guarantee
- Namespace: none; application may check but race possible.
- **Pros:** Zero schema cost.
- **Cons:** Vulnerable to concurrency race: two concurrent `POST /cases` with same `external_ref` in same transaction can both pass `SELECT` guard then both `INSERT` before commit — duplicate slips; `T03-026` bulk import would need serialisation; violates "prefer database-enforced" review instruction; expensive to repair after duplicates exist.

## Recommended Uniqueness Namespace
**Option B for v1 — `UNIQUE (tenant_id, case_type_id, external_ref)` where `external_ref IS NOT NULL`.**

**Rationale:** `external_ref` represents an upstream/import identifier **but** `cases` v1 domain does not yet have `source_system` as first-class (only import jobs do `T03-026`). Per review: *prefer C where domain supports source identity, otherwise B*. For v1, `B` is the appropriate boundary: it gives atomic DB enforcement without requiring a new column, aligns with `P01 Q3` "per type ServiceRequest" wording, preserves tenant isolation `AGENTS.md:67`, and is forward-compatible to `C`. When `T05-058` approved connectors exist and `cases.source_system` is added, a superseding ADR can promote namespace to `C` (`tenant_id, source_system, case_type_id, external_ref`) with migration backfill (see §7).

If future pilot proves single tenant receives same `external_ref` from two distinct upstream systems for same type, the migration to `C` is the escape hatch.

## Atomic Database Enforcement Strategy

**Columns:**
- Add `cases.case_type_id UUID NOT NULL FK → case_types.id` (denormalized from `case_type_version_id` via join at write time; `cases.case_type_version_id` stays immutable version pin). Required because uniqueness must be per logical type, not per version (otherwise `v1` and `v2` of same type would allow duplicate).
- Add `cases.external_ref TEXT NULL` (or reuse existing `data_json` path extraction — but extraction inside `JSON` is non-portable `AGENTS.md:35` `GIN` forbidden; dedicated column is portable and indexable).
- Keep `cases.tenant_id` existing `AGENTS.md:247`.

**Constraint/Index (portable, partial, per `AGENTS.md:35` generic types, `sqlite+aiosqlite` + `postgres`):**
```sql
-- migration: add columns
ALTER TABLE cases ADD COLUMN case_type_id UUID NOT NULL REFERENCES case_types(id);
ALTER TABLE cases ADD COLUMN external_ref TEXT;

-- composite unique, NULL-safe (multiple NULLs allowed)
CREATE UNIQUE INDEX ix_cases_tenant_type_external_ref_unique
  ON cases (tenant_id, case_type_id, external_ref)
  WHERE external_ref IS NOT NULL;

-- for idempotent lookup speed
CREATE INDEX ix_cases_external_ref_lookup
  ON cases (tenant_id, case_type_id, external_ref);
```
Naming per `app/db.py:22`. Alembic reversible `upgrade`/`downgrade` `T01-MIG` `task_bbd86b691a63` + `T06-MIG` `task_c95418f27fbd` portable check `scripts/check_portable_sql.sh`.

**Atomicity:** Uniqueness is enforced **inside the same transaction** as `INSERT` (`T02-005` `create_case.py` same-tx audit+outbox `plan.md:206`). No `SELECT` guard race; DB is arbiter. Application may still do pre-check for nicer error, but must handle `IntegrityError`/`UniqueViolation` as authoritative.

## NULL Behavior
- `external_ref IS NULL` means "no upstream id" — **not part of uniqueness** (`WHERE external_ref IS NOT NULL`). Multiple rows with `NULL` allowed (SQL partial index semantics `postgres`+`sqlite`).
- Empty string not allowed: API validation `interfaces/http/schemas/cases.py` trims and rejects `external_ref == ""` with 422 `validation_failed`; import dry-run `T03-026` treats empty as violation quarantined `T03-027`.
- `NULL` is distinct from `""`.

## Conflict / Idempotency Behavior

- **Duplicate `POST /api/v1/cases` with same `(tenant_id, case_type_id, external_ref)` in overlapping transaction:**
  - DB raises `UniqueViolation` on second `INSERT`/`UPDATE`.
  - Application catches, returns `409 Conflict` with `ErrorResponse{code="conflict", message="external_ref already exists for this case type", details={existing_case_id, existing_reference}, correlation_id}` `interfaces/http/errors.py` `AGENTS.md:59`.
  - No case created, no audit/outbox for duplicate, no stack leak `AGENTS.md:59`.
  - Existing case not leaked cross-tenant (query scoped `WHERE tenant_id=:tenant_id`).

- **Idempotency (`Idempotency-Key` header `interfaces/http/idempotency.py` `T01-004`) is orthogonal:**
  - Same `Idempotency-Key` + same `external_ref` → idempotent replay returns prior `201` with same `reference` (outbox replay `T03-052`).
  - Different `Idempotency-Key` + same `external_ref` → `409` duplicate (not idempotent).

- **Import path `T03-026`:** Bulk CSV/JSONL row violating uniqueness → per-record quarantine `T03-027` `task_43d6ef9b1ae4`, not whole batch failure; dry-run reports `row: external_ref duplicate of existing case <ref>`.

## Import Implications
- `T03-026` `run_import.py` dry-run validates batch-internal duplicates (same file two rows same key) **and** DB-existing duplicates via `SELECT` for reporting, but **authoritative check is DB index at commit**.
- Mapping `MappingVersion` must declare which column maps to `external_ref` (declarative only `P14 Q2` `AGENTS.md:180`).
- Reconciliation report shows `accepted / quarantined(duplicate) / rejected`.

## Migration Implications if Namespace Changes Later

- **From B → C (add `source_system`):**
  1. Add `cases.source_system TEXT NULL REFERENCES connectors(id)` `T05-058`.
  2. Backfill existing `external_ref` rows with `source_system = 'unknown'` or per-import provenance (`T05-059` `coexistence.py`).
  3. `DROP INDEX ix_cases_tenant_type_external_ref_unique`.
  4. `CREATE UNIQUE INDEX ix_cases_tenant_type_source_external_unique ON cases (tenant_id, source_system, case_type_id, external_ref) WHERE external_ref IS NOT NULL;` (or `(tenant_id, source_system, external_ref)` if type not needed per source).
  5. Keep old index name as deprecated for one release with `IF EXISTS` Downgrade.

- **From B → A (broaden to tenant-wide):** Drop B index, create `UNIQUE (tenant_id, external_ref)` — would need deduplication of existing per-type duplicates (quarantine).

- **From B → D (remove):** `DROP INDEX` — trivially reversible, but would re-expose race; not recommended.

- **All migrations reversible** via `alembic downgrade` `T06-MIG`; tenant isolation preserved.

## Tasks/Features Affected
`T02-005` `task_bd788cd29a47` (atomic insert + 409 handling), `T02-008` `task_6acf6097cdcb` (schema `external_ref` flag per type), `T00-DB` `task_186f17d88fcf` + `T01-MIG` `task_bbd86b691a63` (add `case_type_id` + `external_ref` columns), `T03-026` `task_79dbd4387ee6` + `T03-027` `task_43d6ef9b1ae4` (dry-run/quarantine), `T04-009` `task_64e7c50c61cc` (human `reference` vs business `external_ref`), `T04-010` `task_5691a30846b4` (merge must handle duplicate), `T06-MIG` `task_c95418f27fbd`, `T06-TEST` `task_95287b6929e5` (duplicate-create + concurrency test), `T03-048` `AGENTS.md:67` tenant isolation. FM-005, FM-008, FM-009, FM-010, FM-026, FM-027, FM-002.

## DAG Dependency Changes Required
**No new DAG edges required beyond existing** — `T02-005` already depends on `T02-008` `task_6acf6097cdcb` (which will enforce `case_type_id` column), and `T03-026` depends on `T02-005`. The gate on `T02-005` `gate_12403aea296b` already blocks intake until this ADR is re-approved. After re-approval, `T02-005` implementation must be updated to handle `UniqueViolation` → `409` (already in scope). No broadening of implementation while revising per review instruction.

**If future promotion to C is decided, a new migration task `T04-009-C` (or `T05-SOURCE`) would be added depending on `T05-058` — not in this pass.**

## Approval Gate
**Original gate `gate_12403aea296b` on `task_bd788cd29a47` resolved as `approved` at 2026-08-31 16:02:24 — superseded by review `needs_revision`.** This revised ADR remains **Proposed** until a **new gate** (or re-resolve) on `task_bd788cd29a47` is created and resolved as `approved`. Do not mark Accepted until re-approval. A new gate `gate_P01_rev2` should be created gating `T02-005` (or `T02-008` if schema chosen) before implementation proceeds.

## Azure Seam
None directly; uniqueness is schema. If `ADR-021` per-tenant DB were adopted, uniqueness would be per-DB.

---
*Status Proposed (revised) — awaiting re-approval on new gate. Do not mark Accepted until `needs_revision` is cleared with `approved`.*
