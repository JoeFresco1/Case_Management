# ADR-035: SLA authoring validation and breach-escalation visibility (FM-018 / P02)

- Status: Accepted (2026-09-11)
- Scope: `_validate_start_sla_action` in workflow-definition validator, escalation-config forwarding in `execute_transition.py` + `start_workflow.py`, `test_sla_breach_visibility.py` + `test_workflow_sla_validation.py`.
- Supersedes: nothing (completes the FM-018 H1 slice).
- Related: AGENTS.md R10/R12, FM-018 (P02-US6), FM-003, ADR-003 (SLA seam), ADR-013 (FM-018 H0).

## Context

The first slice shipped the SLA scanner (`scan_sla.py`), the `SlaClock`
domain, the `sla_scanner` startup task, the `SlaStateView` in case
summaries, and the `SqlSlaScanner` + `SqlSlaClockRepository`. But the
**publish gate** did not validate `start_sla` action shape (a typo'd
`sla_key`, a non-positive `hours`, an escalation referencing an unknown
queue would only fail at runtime), and the **breach-escalation path**
was untested at the scanner level (no integration between the breach
fire and the audit timeline / task creation).

## Options considered

1. **Leave escalation config unchecked at publish; fail at runtime.**
   - Pros: no validator changes.
   - Cons: a malformed `start_sla` action publishes and only fails
     when a case breaches — the operator discovers the typo under
     pressure. Rejected.

2. **Validate `start_sla` at publish + forward escalation config to the
   clock + scanner-level breach test (chosen).**
   - Pros: config errors surface at authoring; the scanner test proves
     breach → task → timeline audit entry.
   - Cons: one new validator function + a forwarding change in two
     engine call sites.

3. **Implement `notify` escalation fan-out (email, IM).**
   - Pros: real breach notifications.
   - Cons: no transport exists (no mailer, no IM client in the local
     profile); introducing one is P04/P06 scope, not FM-018. Rejected —
     the `notify` type is accepted as a placeholder that the scanner
     reports as `escalation_fired=false`.

## Decision

**Option 2**:

- `_validate_start_sla_action` in
  `app/application/configuration/validate_workflow_definition.py`
  enforces: `sla_key` non-empty string; `hours` positive number;
  `escalation` / `escalation_on_breach` / `escalation_on_warning` are
  JSON objects with `type` in `{"create_task", "notify"}`; a
  `create_task` escalation must name a non-empty `queue`, and when the
  caller supplied `known_queues` the queue must be known — a
  queue-less escalation is rejected at publish (the runtime would fail
  closed, but the operator should learn this at authoring time).
- `execute_transition.py` and `start_workflow.py` forward the inline
  escalation config (`escalation_on_breach` / `escalation_on_warning`,
  with `escalation` as the legacy alias) onto the `SlaClock` at start
  time so the scanner can fire it on breach or warning.
- `test_sla_breach_visibility.py` proves: a breached clock with a
  `create_task` escalation → task row exists → audit timeline entry
  references it (`escalation_fired=true`); a `notify` placeholder fires
  nothing but still audits (`escalation_fired=false`).

## Consequences

- `pytest -k "sla or Sla or SLA"` green (55 tests).
- Publishing a workflow with a malformed `start_sla` action is
  rejected with `422 validation_failed`.
- The scanner's escalation config is validated at publish and forwarded
  to the clock at start — the breach path is deterministic and tested.

## Azure-seam note

No new seam. The scanner runs as an in-process task (local) or an
Azure-hosted worker (production) consuming the same table through the
same use case; the escalation config lives on the clock row.
