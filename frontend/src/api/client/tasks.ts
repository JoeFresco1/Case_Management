/**
 * Task listing, completion, and worker-queue operations (P01 / FM-016).
 */
import type { components } from "../types.gen";
import type { TaskPage, TaskView } from "./types";
import { request } from "./http";

/** List tasks visible to the current actor using the generated API contract. */
export function listTasks(params: {
  assignee?: string;
  status?: "open" | "all";
  limit?: number;
  offset?: number;
} = {}): Promise<TaskPage> {
  const search = new URLSearchParams();
  if (params.assignee !== undefined) search.set("assignee", params.assignee);
  if (params.status !== undefined) search.set("status", params.status);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  const qs = search.toString();
  return request<TaskPage>(`/tasks${qs ? `?${qs}` : ""}`);
}

/** Complete a task idempotently and return its updated canonical view. */
export function completeTask(
  taskId: string,
  body: components["schemas"]["CompleteTaskRequest"] = {},
): Promise<TaskView> {
  return request<TaskView>(`/tasks/${taskId}/complete`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

/**
 * Worker queue operations (FM-016 / P02-US3, US9).
 *
 * Every mutation is audited server-side with the time-bounded
 * delegation window (`until`) echoed on the delegate response so
 * the board can render cover expiry without re-reading audit.
 */
export const queues = {
  /** List queues visible to the current worker (tenant-scoped). */
  listVisible: () =>
    request<components["schemas"]["QueueSummary"][]>("/queues"),
  /** List open tasks on a queue with per-task aging context. */
  listQueueTasks: (queueKey: string) =>
    request<components["schemas"]["QueueTaskView"][]>(
      `/queues/${encodeURIComponent(queueKey)}/tasks`,
    ),
  /** Claim the next open task from a queue for the actor. */
  claimNext: (queueKey: string) =>
    request<components["schemas"]["ClaimNextResponse"]>(
      `/queues/${encodeURIComponent(queueKey)}/claim`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
  /** Release a claimed task back to its queue (claim is reversible). */
  releaseTask: (taskId: string, body: { reason?: string; queue_key?: string } = {}) =>
    request<components["schemas"]["ReleaseTaskResponse"]>(
      `/tasks/${taskId}/release`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
  /** Bulk claim or release a batch of tasks on a queue. */
  bulkHandle: (
    queueKey: string,
    body: { action: "claim" | "release"; task_ids: string[] },
  ) =>
    request<components["schemas"]["BulkHandleResponse"]>(
      `/queues/${encodeURIComponent(queueKey)}/bulk-handle`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
  /**
   * Delegate open tasks to another user for a bounded window.
   * `until` is exclusive: the sweep restores the prior owner once
   * `now > until`. The response echoes `until` for expiry display.
   */
  delegateTasks: (body: {
    delegate_user_id: string;
    task_ids: string[];
    until: string;
  }) =>
    request<components["schemas"]["DelegateResponse"]>("/tasks/delegate", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
};
