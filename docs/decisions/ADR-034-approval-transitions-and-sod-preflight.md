# ADR-034: Approval-gated transitions and separation-of-duties preflight (FM-017 / P02)

- Status: Accepted (2026-09-11)
- Scope: `_validate_approvals` in workflow-definition validator, `_approval_preflight` in transition preflight, audit `sod_outcome` field, `test_approval_sod.py` + `test_preflight_approval.py`.
- Supersedes: nothing (first implementation of the FM-017 slice).
- Related: AGENTS.md R10/R12, FM-017 (P02-US4), FM-016 (queues), ADR-030.

## Context

The first slice shipped consequential workflow transitions (approval
required) and the runtime SoD gate in `execute_transition.py` — originators
are refused, eligible second actors succeed, the audit row carries the
outcome. But the **publish gate** and the **preflight read path** were
silent: a malformed `approval_required` config (automatic = not
human-driven, or an empty role string in `eligible_approver_roles`)
would only fail at runtime; the UI could not preview "needs a second
actor" before the worker attempted the transition.

## Options considered

1. **Inline the checks in the engine only.**
   - Pros: no validator changes.
   - Cons: malformed definitions are published and only fail under load;
     operators learn the rule by crashing, not at authoring time.
     Rejected — the publish gate must catch incoherent config.

2. **Add publish-time validation + preflight SoD surfacing (chosen).**
   - Pros: malformed definitions are rejected at publish; the UI can
     render "needs a second actor" before any mutation; the audit row
     carries `sod_outcome` for traceability.
   - Cons: one new validator function + one preflight helper.

3. **Add a full approval-ticket object.**
   - Pros: richer workflow (pending approval state, approver delegation).
   - Cons: a new domain aggregate is a separate feature, not FM-017. The
     engine model is single-step (the *other* actor executes); a ticket
     system would expand scope. Explicitly out of scope (plan §3 non-goals).

## Decision

**Option 2**:

- `_validate_approvals` in
  `app/application/configuration/validate_workflow_definition.py`
  enforces: (a) `approval_required` transitions must be `human_driven`
  (an automatic approval is a self-approval runaway); (b) every entry
  in `eligible_approver_roles` must be a non-empty string. Roles are
  stringly-checked at this layer because the role registry is
  tenant-configured at runtime (FM-048) and the validator is a pure
  function with no I/O — the engine enforces the gate fail-closed at
  runtime instead. An empty `eligible_approver_roles` tuple means
  "any role with `case.transition` who is not the originator".
- `_approval_preflight` in `preflight_transitions.py` mirrors the
  engine's `_evaluate_separation_of_duties` so the UI can render
  "needs a second actor" before attempting. System actors (role
  `system`) always satisfy the gate. The preflight result carries
  `approval_required`, `approval_eligible`, and `approval_reason`.
- The `WorkflowTransitioned` audit row stamps `sod_outcome` as
  `allowed` / `denied_originator` / `denied_role` / `bypass_system`
  / `not_required`.

## Consequences

- `test_approval_sod.py` (5 tests) + `test_preflight_approval.py` (3
  tests) + `test_workflow_approval_validation.py` green.
- Publishing a workflow with `approval_required` on an automatic
  transition is rejected with `422 validation_failed`.
- Originator `POST /cases/{id}/transitions` on a consequential
  transition gets `403` with a SoD reason; the second actor succeeds.
- No new domain aggregate introduced — the engine remains single-step.

## Azure-seam note

No new seam. The validator is a pure function; the preflight reads
through the same `CaseRepository` / `WorkflowVersionRepository` ports.
The Azure SQL adapter changes only how rows are stored, not how
approval config is authored or evaluated.
