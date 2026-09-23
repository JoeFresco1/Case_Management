# ADR-030: Queue SQL wiring and queue-event catalog registration (FM-016 / FM-055)

- Status: Accepted (2026-09-10)
- Scope: `queues` table, `SqlQueueRepository`, queue HTTP boundary, `EVENT_CATALOG`.
- Supersedes: nothing (completes the T066-T077 follow-up noted in `queues.py`).
- Related: AGENTS.md R1/R10/R12, FM-016 (P02-US3, US9), FM-052, FM-055, ADR-004, ADR-023.

## Context

The first slice shipped the queue domain aggregate, the full worker
use-case surface (`manage_queues`: define / reconfigure / retire /
claim / release / bulk / delegate), and the queue table — but the
HTTP router was a `501` stub ("SQL adapter not yet wired") and the
`SqlQueueRepository` only implemented `list_keys` for workflow
validation. Queue operations were therefore unreachable over HTTP,
and the queue/task lifecycle events the use cases enqueue
(`QueueDefined`, `TaskClaimed`, …) were unknown to `EVENT_CATALOG`,
so the outbox dispatcher would dead-letter them as `unknown_event_type`.

## Options considered

1. **Wire queues through the existing case provider bundle.**
   - Pros: no new provider files.
   - Cons: the case bundle does not carry queue/role ports; stuffing
     them in widens a bundle every case endpoint pays for. Rejected.

2. **Dedicated queue provider + full CRUD repository + catalog registration (chosen).**
   - Pros: mirrors the admin-roles boundary shape; tenant isolation
     and optimistic concurrency live in one place; catalog stays the
     single source of truth for dispatchable event types.
   - Cons: one new migration (`0020`), one new provider file pair.

3. **Teach the dispatcher to pass through unknown event types.**
   - Pros: no catalog edits.
   - Cons: silences use-case bugs — the dead-letter-on-unknown rule is
     the safety net that catches typo'd event types before they reach
     subscribers. Rejected.

## Decision

**Option 2**:

- `queues.version_token` column (migration `0020`, portable
  `ADD COLUMN … DEFAULT '1'`); domain `version_token` enforced via
  compare-and-swap in `SqlQueueRepository.update`.
- `SqlQueueRepository` CRUD in
  `repositories/queue_repository.py`; `workflow_repository.py` keeps a
  re-export so existing imports keep working.
- `QueueHttpRepositories` / `SqlQueueHttpDependencyProvider` bundle
  (queue + task + case-lookup + role + audit + outbox) sharing one
  request transaction; registered as `app.state.queue_http_dependencies`.
- Queues router fully wired: admin CRUD plus worker
  claim / release / bulk-handle / delegate. Claim on an empty queue is
  `404 no_work_available`, not a synthetic task.
- `EVENT_CATALOG` gains `QueueDefined`, `QueueReconfigured`,
  `QueueRetired`, `TaskClaimed`, `TaskReleased`, `TaskBulkHandled`,
  `TaskDelegated` (additive-only per ADR-023; existing versions untouched).

## Consequences

- `POST /api/v1/admin/queues` → claim → release now works end-to-end
  against SQLite and Postgres with no new infrastructure.
- Queue/task events dispatch instead of dead-lettering; subscribers see
  them in `GET /api/v1/events/catalog`.
- `release_task` accepts an optional `queue_key` fallback: a claim
  moves the task off its queue (`assignee_queue_id = None`), so a
  claimed task carries no return address. The stored prior queue wins
  when present; the caller-supplied key covers the claimed case.
  Without either, release still fails closed with `InvariantViolation`.
- Follow-up (not in this change): queue admin UI in `frontend/src`,
  capacity-based saturation guard in the transition engine.

## Azure-seam note

No new seam. The SQL repository is the local adapter behind the
`QueueRepository` port; the Azure SQL adapter implements the same port
later. The dispatcher logic is adapter-independent.
