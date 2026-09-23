# ADR-043: Run E forms convergence (FM-036/038 binding lifecycle, derived write-time compute, single-format designer)

- Status: Accepted (2026-09-21)
- Scope: FM-036, FM-038, FM-037, FM-012 convergence for Run E (T3 + T2-meta).
- Related: FM-036, FM-038, FM-037, FM-012, ADR-033, ADR-012, P07/pillar.md, P05/pillar.md.

## Context

E-1 through E-7 built the forms runtime incrementally: conditional definitions with
publish-time validation, single-step submit with hidden-field preservation, then
multi-step sessions with CAS persistence and the state-bound transition guard.
Three classification questions stayed open at convergence: (1) is the binding
lifecycle complete on both versions *and* bindings; (2) where do derived values
compute and what do renames preserve; (3) is the visual designer a second runtime
or the same format with a visual editor.

## Decision

1. **Bindings retire, not just versions (FM-036).** `FormBinding.retire()` (domain)
   + `SqlFormBindingRepository.mark_retired()` (infrastructure, migration `0027`)
   close the lifecycle to `draft → published → retired` on bindings. Retired
   bindings refuse new submissions via `assert_applies_to_case`; cases stay pinned
   via `case_type_version_id` (G4). No silent reactivation: retire is irreversible
   in both layers.
2. **Derived fields compute at write time; identity is the stable key (FM-012).**
   `compute_derived_fields` runs in `submit_form.handle` (and intake paths) after
   the deterministic pipeline: caller-supplied derived values are dropped, missing
   operands resolve to `None` (never raise on partial drafts), `div/0` raises
   `InvariantViolation`, and publish-gate validation rejects undefined operands.
   Label renames preserve the stable machine `key` with ordered `label_history`
   (no-op and unsorted histories rejected). No stored procedures, no read-time
   recomputation contract, no key rewrite on rename.
3. **Single-format designer (FM-037).** `VisualFormDesigner.tsx` emits exactly the
   `FormDefinition` JSON the runtime validates (`_validate_definition`) and
   `FormVersion.draft` snapshots. The contract is pinned on both sides
   (`tests/domain/test_form_designer_contract.py` + `formDefinitionContract.test.ts`
   + `VisualFormDesigner.test.tsx`); either side diverging fails loudly. The
   designer has no persistence of its own, no second schema, and cannot express
   arbitrary code (whitelisted operators, tab-order/own-ref/cycle guards).
4. **No new Azure seam.** Forms reuse seams 1–2 (SQLAlchemy repos, FS
   `DocumentStore`) plus the P14 lookup/signature/attachment connector slots.
   Lookup/signature dependency failures fail closed with rollback (nothing staged).

## Consequences

- OpenAPI forms surface is stable: submit + version/bindings reads + session
  start/save/resume + transition check, all `Idempotency-Key`/`If-Match` guarded
  where mutating.
- Known debt carried (not Run E gaps): `models_forms.py` is dead code (live form
  tables in `models.py`); `form_definitions/versions/bindings` tables lack
  migrations (tests run on `create_all`); `routers/forms.py:358-365` header still
  says "FM-038 deferred" although ADR-033 wired the SQL adapter — reword in a
  follow-up (minimal-scope rule left it untouched here).
- Gate debt is explicit in `architecture-runE.md §8`: `ruff check` (13 Run E
  findings in `app/domain/form.py`) and `ruff format --check` (incl.
  `app/domain/form.py`) fail; everything else verified green or exactly reported.

## Azure-seam note

No seam added or flipped. When corporate e-sign/lookup sources arrive, they plug
the existing `signature_connector` / `lookup` callables behind the same Protocols
(§12 pattern: Protocol → local adapter → placeholder dir → `config`/`deps` wiring
→ startup log + `/api/v1/health`); the deterministic core stays LLM/SDK-free per
Constitution XIV.
