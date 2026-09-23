# ADR-025 — P05 Incompatible Metadata Type-Change Semantics

**Status:** Accepted — approved via gate `gate_08f7544512b9` on `task_6acf6097cdcb` `T02-008` at 2026-08-31 16:00:45 (`approved`)  
**Date:** 2026-08-31  
**Accepted:** 2026-08-31 (gate `gate_08f7544512b9` → `approved`)  
**Owner:** Platform team  
**Related Pillars:** P05 Metadata & Indexing, P01 Case & Business Object Management  
**Pillar Questions:** `specs/pillars/P05-metadata-indexing/pillar.md:8` Q1 (enum versioning), Q2 (incompatible type change)  
**Tasks/Features Affected:** FM-008 `T02-008` `task_6acf6097cdcb`, FM-012 `T05-012` `task_64749a43eebd`, FM-007 `T02-007` `task_c3a2124aec9c`, `backend/app/domain/case_type.py:1`, `backend/app/application/cases/compute_derived.py`, `AGENTS.md:272`

## Context
Case types are versioned `CaseTypeVersion` with `schema_json` `AGENTS.md:272` `{fields:[{key,label,type,required,sensitivity,searchable}], document_classifications, rules}`. Stable `key` is identity; `label` may rename per `P05-US8` `AGENTS.md:285`. Published versions are immutable `case_type.py:publish()`. `P05 Q2` asks: when a field's type changes incompatibly (e.g., `text`→`integer`), is automatic migration attempted? `AGENTS.md:285` says stable key is identity; `P05 Q1` asks enum values versioning when they change (value cannot be removed if any case uses it).

Existing constraint: Immutability on publish is enforced `T02-008`; validation at write time only `case.py:171` `AGENTS.md:285`. No automatic migration exists in `spec.md`/`plan.md`.

## Decision Required
What happens to existing cases bound to old versions when a new `CaseTypeVersion` changes a field incompatibly?

## Viable Options

### Option A — New field, old preserved (current implied, recommended)
- Incompatible change → create **new field key** (e.g., `due_date_v2` type `date` replacing `due_date` type `text`); old `due_date` remains in historical cases under old version; `schema_json` of new version contains new key; no data motion; `T02-007` edit uses new key only for new cases; `T05-012` derived fields map old→new explicitly.
- **Pros:** Preserves history `AGENTS.md:285`; no silent data loss; published immutability honored; reversible; portable `JSON` `AGENTS.md:35` no type coercion.
- **Cons:** Schema bloat (old keys linger); UI must handle missing new field for old cases; query over mixed versions needs union.

### Option B — In-place type coercion with migration
- New version redefines same `key` with new `type`; migration attempts `CAST` for existing rows; failures quarantined `T03-027`.
- **Pros:** Cleaner schema (one key); queries simple.
- **Cons:** Requires data motion (`UPDATE cases SET data_json...`); `AUTOINCREMENT` lock on `cases` `AGENTS.md:247`; non-portable `JSON` path coercion; audit history `T01-003` chain would need `AuditCorrectionRecorded` for every migrated case; high risk of silent truncation; irreversible without compensating.

### Option C — Versioned field registry with explicit mapping
- Introduce `FieldRegistry` versioned separately from `CaseTypeVersion`; `case.data_json` stores `field_key@version`; mapping `old_text→new_int` is declarative, not automatic, validated at publish.
- **Pros:** Most explicit; supports `T05-012` derived fields cleanly.
- **Cons:** Adds new table/artifact `field_versions` not in canonical `AGENTS.md:242`; over-engineering for v1 slice `tasks.md:9`; horizontal infrastructure before vertical slice.

## Consequences / Tradeoffs
| Option | Schema/Migration | Public Contract | Tenant Isolation | Event Compatibility | Reversibility |
|---|---|---|---|---|---|
| A | No migration (new key) | Stable `key` contract preserved `AGENTS.md:285` | None | Events carry `case_type_version_id`, so `CaseCreated` versioned per schema | Reversible — old key still queryable |
| B | `UPDATE` migration, lock, quarantine | Same `key` but type changes — breaking for consumer | None | Events would need re-emit | Hard — data loss not replayable |
| C | New registry table | New contract `field@version` | None | Events need registry version | Medium — registry can be deprecated |

## Recommended Option
**Option A — New field key, old preserved.**

## Why Recommended
- Aligns with explicit `AGENTS.md:285` "Stable `key` is the identity; `label` may rename without breaking history; validation at write time only" and `P05 Q1` intent "enum value cannot be removed if any case uses it" — same principle for type.
- Honors published immutability `T02-008` and audit explainability `T01-003`; no migration means no `AuditCorrectionRecorded` flood.
- Keeps `T00-DB` portable `JSON` `AGENTS.md:35`; no dialect-specific `CAST` inside `JSON`.
- Supports incremental replacement `constitution.md XI` without big-bang.

## Reversibility / Migration Implications
- **Reversible now:** Using new key imposes no migration; reverting is delete new key from draft.
- **Irreversible later if B chosen:** First migration that coerces `text→int` would touch every row of matching type; rollback requires compensating (new version reverting key) + data re-quarantine; gate before `T02-008` publish to avoid expensive backfill.

## Tasks/Features Affected
`T02-008` `task_6acf6097cdcb` (publish validation: reject in-place incompatible type unless new key), `T05-012` `task_64749a43eebd` (derived fields, durable identity, label rename), `T02-007` `task_c3a2124aec9c` (edit preserves history), `T04-009` `task_64e7c50c61cc` (stable key reference). FM-008, FM-012, FM-007, FM-009.

## Approval Gate
**Created:** `gate_P05_type_change` on `task_6acf6097cdcb` `T02-008` (see report). Blocks schema publication until semantics are approved; `T05-012` depends on it transitively.

## Existing Constraint Recorded
Existing constraints: published immutability `T02-008`, stable key identity `AGENTS.md:285` — reaffirmed, not reopened. A future automated migration would need a superseding ADR + funded slice.

## Azure Seam
None; but if `Option B` were pursued, Azure SQL `JSON` coercion dialect differs — another portability reason for `A`.

---
*Status Accepted via gate `gate_08f7544512b9` (`approved`). Do not revert without superseding ADR.*
