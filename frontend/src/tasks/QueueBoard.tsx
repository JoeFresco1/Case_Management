import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import styles from "../cases/CaseWorkspace.module.css";

type QueueSummary = components["schemas"]["QueueSummary"];
type QueueTaskView = components["schemas"]["QueueTaskView"];

interface DelegateState {
  delegateUserId: string;
  untilDays: string;
}

/**
 * Worker queue board (FM-016 / P02-US3, US9).
 *
 * Renders every queue visible to the worker with its open tasks and
 * per-task aging context (`is_overdue`, `overdue_by_seconds`), plus
 * claim / release / bulk / delegate actions:
 *
 * - claim-next hands the actor the most-urgent open task;
 * - release returns a claimed task to its queue (reversible claim);
 * - delegate hands tasks to another user for a bounded window —
 *   the response echoes `until` so the board renders cover expiry
 *   without re-reading the audit row. Delegation is audited
 *   server-side (`TaskDelegated` with `window_seconds`).
 */
export function QueueBoard() {
  const queryClient = useQueryClient();
  const [selectedQueue, setSelectedQueue] = useState<string | null>(null);
  const [delegate, setDelegate] = useState<DelegateState>({
    delegateUserId: "",
    untilDays: "7",
  });
  const [selectedTasks, setSelectedTasks] = useState<string[]>([]);

  const queuesQuery = useQuery({
    queryKey: ["queues", "visible"],
    queryFn: () => api.queues.listVisible(),
    staleTime: 15_000,
  });

  const tasksQuery = useQuery({
    queryKey: ["queues", "tasks", selectedQueue],
    queryFn: () => api.queues.listQueueTasks(selectedQueue as string),
    enabled: selectedQueue !== null,
    staleTime: 10_000,
  });

  const invalidateBoard = () => {
    void queryClient.invalidateQueries({ queryKey: ["queues"] });
    void queryClient.invalidateQueries({ queryKey: ["tasks", "list"] });
  };

  const claim = useMutation({
    mutationFn: (queueKey: string) => api.queues.claimNext(queueKey),
    onSuccess: () => invalidateBoard(),
  });

  const release = useMutation({
    mutationFn: (taskId: string) => api.queues.releaseTask(taskId, {}),
    onSuccess: () => invalidateBoard(),
  });

  const bulk = useMutation({
    mutationFn: (vars: {
      queueKey: string;
      action: "claim" | "release";
      taskIds: string[];
    }) =>
      api.queues.bulkHandle(vars.queueKey, {
        action: vars.action,
        task_ids: vars.taskIds,
      }),
    onSuccess: () => {
      setSelectedTasks([]);
      invalidateBoard();
    },
  });

  const delegateTasks = useMutation({
    mutationFn: () => {
      const until = new Date(
        Date.now() + Number(delegate.untilDays) * 24 * 60 * 60 * 1000,
      ).toISOString();
      return api.queues.delegateTasks({
        delegate_user_id: delegate.delegateUserId,
        task_ids: selectedTasks,
        until,
      });
    },
    onSuccess: () => {
      setSelectedTasks([]);
      invalidateBoard();
    },
  });

  if (queuesQuery.isLoading) return <p>Loading queues…</p>;
  if (queuesQuery.error)
    return (
      <p className={styles.error}>
        {queuesQuery.error instanceof ApiClientError
          ? queuesQuery.error.message
          : "Failed to load queues."}
      </p>
    );

  const queues: QueueSummary[] = queuesQuery.data ?? [];
  const tasks: QueueTaskView[] = tasksQuery.data ?? [];
  const lastDelegation = delegateTasks.data;

  return (
    <section aria-labelledby="queue-board-heading">
      <h1 id="queue-board-heading">Queue board</h1>
      <p className={styles.meta}>
        Claim, release, and delegate work with due-date aging context.
        Delegation is always time-bounded — the cover expiry is shown
        after each delegation and recorded in audit.
      </p>

      {queues.length === 0 ? (
        <p className={styles.empty}>No queues are visible to you.</p>
      ) : (
        <ul className={styles.transitionList}>
          {queues.map((queue) => (
            <li key={queue.id} className={styles.transitionItem}>
              <div>
                <strong>
                  <code>{queue.key}</code>
                </strong>{" "}
                — {queue.name}
                <div className={styles.meta}>
                  {queue.assignment_policy} ·{" "}
                  {queue.member_user_ids.length} member(s)
                </div>
              </div>
              <div>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedQueue(queue.key);
                    setSelectedTasks([]);
                  }}
                  aria-pressed={selectedQueue === queue.key}
                >
                  {selectedQueue === queue.key ? "Selected" : "View tasks"}
                </button>{" "}
                <button
                  type="button"
                  disabled={claim.isPending}
                  onClick={() => claim.mutate(queue.key)}
                  title={`Claim the next open task from ${queue.key}`}
                >
                  {claim.isPending ? "Claiming…" : "Claim next"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {claim.error instanceof ApiClientError ? (
        <p className={styles.error} role="alert">
          {claim.error.message}
        </p>
      ) : null}

      {selectedQueue !== null ? (
        <section aria-label={`Tasks on ${selectedQueue}`}>
          <h2>
            Open tasks on <code>{selectedQueue}</code>
          </h2>
          {tasksQuery.isLoading ? (
            <p>Loading tasks…</p>
          ) : tasksQuery.error ? (
            <p className={styles.error}>
              {tasksQuery.error instanceof ApiClientError
                ? tasksQuery.error.message
                : "Failed to load queue tasks."}
            </p>
          ) : tasks.length === 0 ? (
            <p className={styles.empty}>No open work on this queue.</p>
          ) : (
            <>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th aria-label="Select">✓</th>
                    <th>Title</th>
                    <th>Status</th>
                    <th>Age / due</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => {
                    const checked = selectedTasks.includes(task.task_id);
                    return (
                      <tr
                        key={task.task_id}
                        className={
                          task.aging.is_overdue ? styles.errorRow : undefined
                        }
                      >
                        <td>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) =>
                              setSelectedTasks((prev) =>
                                event.target.checked
                                  ? [...prev, task.task_id]
                                  : prev.filter((id) => id !== task.task_id),
                              )
                            }
                            aria-label={`Select ${task.title}`}
                          />
                        </td>
                        <td>
                          <a href={`/cases/${task.case_id}`}>{task.title}</a>
                          <div className={styles.meta}>
                            <code>{task.type}</code>
                          </div>
                        </td>
                        <td>{task.status}</td>
                        <td>
                          {task.aging.is_overdue ? (
                            <span className={styles.error}>
                              Overdue
                              {task.aging.overdue_by_seconds !== null &&
                              task.aging.overdue_by_seconds !== undefined
                                ? ` by ${formatDuration(task.aging.overdue_by_seconds)}`
                                : ""}
                            </span>
                          ) : task.aging.due_in_seconds !== null &&
                            task.aging.due_in_seconds !== undefined ? (
                            <span>
                              Due in {formatDuration(task.aging.due_in_seconds)}
                            </span>
                          ) : (
                            <span>
                              Age {formatDuration(task.aging.age_seconds)}
                            </span>
                          )}
                        </td>
                        <td>
                          <button
                            type="button"
                            disabled={release.isPending}
                            onClick={() => release.mutate(task.task_id)}
                            title="Release this task back to its queue"
                          >
                            Release
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              <div className={styles.actions}>
                <button
                  type="button"
                  disabled={selectedTasks.length === 0 || bulk.isPending}
                  onClick={() =>
                    selectedQueue &&
                    bulk.mutate({
                      queueKey: selectedQueue,
                      action: "claim",
                      taskIds: selectedTasks,
                    })
                  }
                >
                  Bulk claim ({selectedTasks.length})
                </button>{" "}
                <button
                  type="button"
                  disabled={selectedTasks.length === 0 || bulk.isPending}
                  onClick={() =>
                    selectedQueue &&
                    bulk.mutate({
                      queueKey: selectedQueue,
                      action: "release",
                      taskIds: selectedTasks,
                    })
                  }
                >
                  Bulk release ({selectedTasks.length})
                </button>
              </div>

              <fieldset className={styles.fieldset}>
                <legend>
                  Delegate selected ({selectedTasks.length}) for a bounded
                  window
                </legend>
                <label className={styles.label}>
                  Delegate user id (UUID)
                  <input
                    className={styles.input}
                    value={delegate.delegateUserId}
                    onChange={(event) =>
                      setDelegate((prev) => ({
                        ...prev,
                        delegateUserId: event.target.value,
                      }))
                    }
                    placeholder="delegate user UUID"
                  />
                </label>{" "}
                <label className={styles.label}>
                  Cover window (days)
                  <input
                    className={styles.input}
                    type="number"
                    min={1}
                    max={90}
                    value={delegate.untilDays}
                    onChange={(event) =>
                      setDelegate((prev) => ({
                        ...prev,
                        untilDays: event.target.value,
                      }))
                    }
                  />
                </label>{" "}
                <button
                  type="button"
                  disabled={
                    selectedTasks.length === 0 ||
                    !delegate.delegateUserId ||
                    delegateTasks.isPending
                  }
                  onClick={() => delegateTasks.mutate()}
                >
                  {delegateTasks.isPending ? "Delegating…" : "Delegate"}
                </button>
                {lastDelegation ? (
                  <p role="status" className={styles.meta}>
                    Delegated {lastDelegation.delegated.length} task(s) to{" "}
                    <code>{lastDelegation.delegate_user_id}</code> until{" "}
                    <time dateTime={lastDelegation.until}>
                      {new Date(lastDelegation.until).toLocaleString()}
                    </time>{" "}
                    — expiry restores the prior owner and is audited.
                  </p>
                ) : null}
              </fieldset>

              {bulk.error instanceof ApiClientError ? (
                <p className={styles.error} role="alert">
                  {bulk.error.message}
                </p>
              ) : null}
              {delegateTasks.error instanceof ApiClientError ? (
                <p className={styles.error} role="alert">
                  {delegateTasks.error.message}
                </p>
              ) : null}
            </>
          )}
        </section>
      ) : null}
    </section>
  );
}

function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export default QueueBoard;
