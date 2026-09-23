# Pillar P02 — Workflow, Tasks & Queues

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Work moves through explicit, versioned, deterministic workflow. Tasks and queues are first-class.

## 1. Pillar statement

This pillar owns the **engine** that moves a case from one state to another, the **tasks** that result from those transitions, and the **queues** that distribute work to humans. Workflow is a first-class, versioned, immutable configuration artifact (P10). Execution is deterministic (Constitution XIV) and the engine rejects workflow definitions that could be non-terminating or that have ambiguous paths.

A workflow transition is the unit of change. Every transition:

- is requested by an actor (user or system identity),
- is evaluated against the published workflow version bound to the case,
- may be guarded by declarative conditions (P10 rules),
- may fire actions (create task, start SLA, set field),
- writes a `WorkflowTransitioned` audit event (P09) in the same transaction,
- emits a domain event (P13) via the outbox.

## 2. Scope

### In scope
- Workflow definition format (custom JSON, validated, versioned, immutable on publish).
- Workflow engine: state machine with explicit transitions, guards, actions.
- Tasks: assignment, status, completion, idempotency.
- Queues: assignment policies (manual + round-robin v1), membership, capacity.
- SLA clocks: start, pause, resume, breach.
- Approval / separation-of-duties constraints.
- Human-driven workflow cycles (allowed).
- Non-terminating automatic cycles (rejected at publish time).
- Exception paths: explicit `exception_queue` rather than silent failure.
- Workflow simulator: deterministic, returns step trace with guard results.

### Out of scope (deferred)
- Subprocesses / reusable workflow fragments (P3).
- Parallel gateway semantics (P3 — v1 supports sequential and one-task-at-a-time).
- Workflow marketplace or importable workflow packs (P3).
- Per-tenant workflow versioning policy (P3).

### Explicitly not building
- A "code in the workflow" path. Workflows and rules are declarative; no arbitrary code in the admin path (Constitution IX).
- A BPMN 2.0 engine. We are deliberately not BPMN-shaped; the format is custom and lean.
- A "smart" auto-completer that uses AI to advance a case without human review (Constitution XIV).

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `WorkflowDefinition` | A named workflow (e.g., "ServiceRequest"). | Yes |
| `WorkflowVersion` | An immutable, published snapshot. | Yes |
| `WorkflowInstance` | Execution of a WorkflowVersion for one Case. | Yes |
| `Transition` | A configured allowed move from one state to another. | Yes |
| `Task` | A unit of work assigned to a user, queue, or system. | Yes (Phase 2) |
| `Queue` | A named work container with assignment policy. | Yes (Phase 2) |
| `SlaClock` | A measurable deadline with breach state. | Yes (Phase 3) |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P02-US1**: As a case worker, when I complete a task whose configured transition guard passes, the case transitions to the next state, follow-on tasks are created, and a `WorkflowTransitioned` event is emitted — all in one transaction.
- **P02-US2**: As a process administrator, I can draft a workflow, validate it (no unreachable states, no ambiguous transitions, no non-terminating automatic cycles), and publish it as an immutable version. Cases already using a prior version continue to use that prior version.

### P2 — Important
- **P02-US3**: As a case worker, I see my queue with due-date, priority, and aging, and I can claim or release work; bulk operations are explicit and audit-logged.
- **P02-US4**: As a process administrator, I can require an approval transition that enforces separation of duties (the approver must be a different identity from the originator).
- **P02-US5**: As a process administrator, I can simulate a draft workflow against a synthetic case and see the step trace, guard outcomes, and any blockers before publishing.
- **P02-US6**: As a case worker, when an SLA is breached, the configured escalation fires (notification, supervisor task, queue move, exception state) and is visible in the timeline.

### P3 — Deferred
- **P02-US7**: As a process administrator, I can compose a workflow from reusable subprocess fragments.
- **P02-US8**: As a process administrator, I can configure parallel branches with join semantics.
- **P02-US9**: As a case worker, I can delegate my open tasks to another identity (e.g., during PTO) for a bounded period.

## 5. Cross-cuts

- **P01 (Case)**: a WorkflowInstance is bound to a Case; the case's `current_workflow_state` is the pointer.
- **P05 (Metadata)**: transition guards and actions reference case data and metadata by stable field keys.
- **P09 (Audit)**: every transition writes a `WorkflowTransitioned` event.
- **P10 (Administration)**: WorkflowDefinitions and WorkflowVersions are configuration artifacts owned by P10.
- **P11 (Security)**: transitions enforce actor permissions and separation-of-duties.
- **P13 (Events)**: transitions emit domain events through the outbox.

## 6. Acceptance scenarios (representative)

- B1: A case in a state with an allowed transition fires when the user with permission requests it. All required guards, task operations, SLA effects, and the audit event commit consistently; the event/outbox record is created.
- B2: A case's transition is blocked by an unmet required document. The system rejects the request with a structured error identifying the missing requirement and does not advance the case.
- B3: A draft workflow with an unreachable state is rejected at publish time with a clear validation error.
- B4: A draft workflow containing a human-driven cycle (`in_review → needs_info → in_review`) is accepted. A draft workflow composed of automatic transitions that loops without a breakable guard is rejected as a potential runaway.
- B5: A task completion request with the same idempotency key is sent twice. The task completes once; the second request returns the prior result.
- B6: A workflow simulator runs 50 synthetic scenarios against a draft workflow. The report shows pass/fail counts and identifies any state that traps cases with no exit.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements P02-US1 (via tasks + transitions), US2, US5, US6 (basic SLA). |
| `002-leftovers` Run B | Draft | Queue operations over HTTP (FM-016 / P02-US3, US9), second-actor approvals + SoD preflight (FM-017 / P02-US4), SLA authoring validation + warning/breach escalation visibility (FM-018 / P02-US6). See `specs/002-leftovers/architecture-runB.md`, `docs/decisions/ADR-030-queue-sql-wiring-and-event-catalog.md`, `ADR-034-approval-transitions-and-sod-preflight.md`, `ADR-035-sla-authoring-and-breach-visibility.md`, `ADR-038-runB-like-search-and-sla-semantics.md`. |
| Future slice 2 | Planned | Approval / separation of duties (US4), advanced queue policies. |
| Future slice 3 | Planned | Subprocesses, parallel branches, delegation. |

## 8. Open questions

- Q1: For v1, do we support timers and escalation in the first slice or only when a task is breached? Current intent: SLA v1 = deadline + breach action; timers as a separate explicit feature later.
- Q2: How many concurrent tasks may a single case hold? Current intent: unbounded but typically small; constraint enforced in P10.
- Q3: When a transition fires multiple actions and one fails, do we still write the audit event? (Current answer: no — atomicity is the whole point.)
