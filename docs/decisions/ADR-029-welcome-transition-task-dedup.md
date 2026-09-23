# ADR-029: Welcome vs transition task double-fire prevention

- Status: Accepted (2026-09-10)
- Scope: `app/application/workflow/execute_transition.py` create_task materialization loop.
- Supersedes: nothing.
- Related: AGENTS.md R3/R10, T070/T071, FM-014, P02-US1.

## Context

The first slice's workflow vertical has two mechanisms that materialize tasks from the same `create_task` workflow action:

1. **Welcome tasks** (T070): `start_workflow.handle` creates initial tasks by iterating transitions out of the workflow's initial state and staging `create_task` actions for each. The task's `created_by_transition` is set to `"initial"`.

2. **Transition tasks** (T071): `execute_transition.handle` fires the same `create_task` action when the transition actually executes (e.g., `submit_for_review` moves `intake → in_review` and also materializes a review task).

With the ServiceRequest blueprint (intake → in_review → decided → closed), the `submit_for_review` transition has a `create_task` action for a "review" task assigned to `review_queue`. This means the "Review case" welcome task is created at case creation, and a second identical "Review case" task would be created when the transition fires — a duplicate.

The SLA action (`start_sla`) already avoids this via a live-clock check in `_fire_start_sla_actions` (execute_transition.py:941-944): "A clock whose key already has a live clock on this case is left alone... the welcome-task path in `start_workflow` already staged one at intake". But `create_task` had no analogous guard.

## Options considered

1. **Remove `create_task` from transition actions entirely** — rely only on welcome tasks for initial work.
   - Pros: simplest; no duplication possible.
   - Cons: inflexible. Some workflows legitimately create new follow-on tasks at transition time (not just welcome tasks). The `create_task` action type is the canonical way to express this in the workflow schema.

2. **Skip creation if an open task of the same `type` already exists on the case**.
   - Pros: preserves transition-time `create_task` for genuine follow-ons (distinct types), but avoids double-fire of the welcome task. Matches the SLA live-clock pattern. Pure function of existing task state — no schema change.
   - Cons: "type" as dedup key could be too coarse if a workflow intentionally creates multiple open tasks of the same type from different transitions (rare, but possible). Mitigated by the fact that the welcome path only fires once per case.

3. **Add a dedicated `idempotency_key` field to `create_task` actions** (workflow schema change).
   - Pros: explicit, author-controlled.
   - Cons: requires workflow authoring changes, schema version bump, migration. Over-engineering for v1.

## Decision

**Option 2**: in the `execute_transition` create_task loop, read all currently-open tasks for the case (`_tasks_for_case`, which uses `TaskRepository.list_for_case`), collect their `type` values, and skip any action whose `task_type` is already present among open tasks.

- This preserves the welcome task as the canonical instance.
- Genuine transition-time follow-on tasks (different `type`) materialize normally.
- The dedup is per-case, per-type, at the moment of transition execution — no schema changes, no migration.
- The `created_by_transition` field on the Task already distinguishes welcome (`"initial"`) from transition-fired (`"submit_for_review"`, etc.) for audit/timeline purposes.

This matches the existing SLA pattern and keeps the deterministic core free of idempotency complexity (the HTTP layer's `Idempotency-Key` is the canonical replay mechanism; domain idempotency is a side benefit, not the primary contract).

## Consequences

- **Positive**: No duplicate "Review case" tasks for the canonical vertical. The timeline shows one task created at intake.
- **Neutral**: Workflows that intentionally create multiple same-type open tasks from different transitions will only get the first one. This is acceptable for v1 (no such workflows exist in the blueprint).
- **Negative**: None identified.

## Azure seam note

None — this is pure application logic with no infrastructure dependency.