import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";

import { api, ApiClientError } from "../api/client";
import type { TaskView } from "../api/client";
import styles from "./Worker.module.css";

export interface TaskListProps {
  assignee?: "me" | `queue:${string}`;
  statusFilter?: "open" | "all";
  limit?: number;
}

/** Worker queue list with server-side assignee/status filters and aging cues. */
export function TaskList(props: TaskListProps = {}) {
  const status = props.statusFilter ?? "open";
  const limit = props.limit ?? 50;
  const query = useQuery({
    queryKey: [
      "tasks",
      "list",
      { assignee: props.assignee ?? null, status, limit },
    ],
    queryFn: () => api.listTasks({ assignee: props.assignee, status, limit }),
    staleTime: 15_000,
  });
  if (query.isLoading)
    return (
      <p className={styles.empty} role="status">
        Loading tasks…
      </p>
    );
  if (query.error)
    return (
      <p className={styles.error} role="alert">
        {query.error instanceof ApiClientError
          ? query.error.message
          : "Failed to load tasks."}
      </p>
    );
  const tasks = query.data?.items ?? [];
  if (tasks.length === 0)
    return <p className={styles.empty}>No tasks match these filters.</p>;
  const now = new Date();
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Title</th>
            <th>Case</th>
            <th>Status</th>
            <th>Priority</th>
            <th>Due</th>
            <th>Assignee</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => {
            const overdue =
              task.status === "open" &&
              task.due_at &&
              new Date(task.due_at) < now;
            return (
              <tr
                key={task.id}
                className={overdue ? styles.errorRow : undefined}
              >
                <td>
                  <Link to={`/tasks/${task.id}`}>{task.title}</Link>
                  <small className={styles.type}>{task.type}</small>
                </td>
                <td>
                  <Link
                    to={`/cases/${task.case_id}`}
                    aria-label={`Open case ${task.case_id}`}
                  >
                    <code>{task.case_id.slice(0, 8)}</code>
                  </Link>
                </td>
                <td>
                  <span className={styles.badge} data-status={task.status}>
                    {task.status}
                  </span>
                </td>
                <td className={styles.capitalize}>{task.priority}</td>
                <td className={overdue ? styles.overdue : undefined}>
                  {overdue && <strong>Overdue · </strong>}
                  {task.due_at ? new Date(task.due_at).toLocaleString() : "—"}
                </td>
                <td>
                  {task.assignee_user_id
                    ? `@${task.assignee_user_id.slice(0, 8)}`
                    : task.assignee_queue_id
                      ? `queue:${task.assignee_queue_id}`
                      : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Route-level task surface: filter controls, list, and list-backed detail. */
export function TaskPage() {
  const { taskId } = useParams<{ taskId?: string }>();
  const [status, setStatus] = useState<"open" | "all">("open");
  const [assignee, setAssignee] = useState<"me" | "all">("me");
  const detailQuery = useQuery({
    queryKey: ["tasks", "list", { status: "all", limit: 200 }],
    queryFn: () => api.listTasks({ status: "all", limit: 200 }),
    enabled: Boolean(taskId),
    staleTime: 15_000,
  });
  if (taskId) {
    if (detailQuery.isLoading) return <p>Loading task…</p>;
    if (detailQuery.error)
      return (
        <p className={styles.error} role="alert">
          {detailQuery.error instanceof ApiClientError
            ? detailQuery.error.message
            : "Failed to load task."}
        </p>
      );
    const task = detailQuery.data?.items.find((item) => item.id === taskId);
    return task ? (
      <TaskDetail key={task.id} task={task} />
    ) : (
      <p className={styles.empty}>Task not found.</p>
    );
  }
  return (
    <section className={styles.page}>
      <header className={styles.heading}>
        <h1>Task inbox</h1>
        <Link to="/queues">
          View queues <span aria-hidden="true">→</span>
        </Link>
      </header>
      <div className={styles.card}>
        <form
          className={styles.filters}
          aria-label="Task filters"
          onSubmit={(event) => event.preventDefault()}
        >
          <label>
            Assignee{" "}
            <select
              value={assignee}
              onChange={(event) =>
                setAssignee(event.target.value as "me" | "all")
              }
            >
              <option value="me">My tasks</option>
              <option value="all">All tasks</option>
            </select>
          </label>{" "}
          <label>
            Status{" "}
            <select
              value={status}
              onChange={(event) =>
                setStatus(event.target.value as "open" | "all")
              }
            >
              <option value="open">Open</option>
              <option value="all">All statuses</option>
            </select>
          </label>
        </form>
        <TaskList
          assignee={assignee === "me" ? "me" : undefined}
          statusFilter={status}
        />
      </div>
    </section>
  );
}

/** Render a task detail and perform its idempotent completion action. */
export function TaskDetail({ task }: { task: TaskView }) {
  const queryClient = useQueryClient();
  const [currentTask, setCurrentTask] = useState(task);
  const complete = useMutation({
    mutationFn: () => api.completeTask(currentTask.id),
    onSuccess: (completed) => {
      setCurrentTask(completed);
      void queryClient.invalidateQueries({ queryKey: ["tasks", "list"] });
    },
  });
  return (
    <article className={styles.page}>
      <p>
        <Link to="/tasks">← Back to tasks</Link>
      </p>
      <h1>{currentTask.title}</h1>
      <div className={styles.detail}>
        <dl>
          <dt>Type</dt>
          <dd>{currentTask.type}</dd>
          <dt>Status</dt>
          <dd>{currentTask.status}</dd>
          <dt>Priority</dt>
          <dd>{currentTask.priority}</dd>
          <dt>Case</dt>
          <dd>
            <Link to={`/cases/${currentTask.case_id}`}>
              {currentTask.case_id}
            </Link>
          </dd>
          <dt>Due</dt>
          <dd>
            {currentTask.due_at
              ? new Date(currentTask.due_at).toLocaleString()
              : "—"}
          </dd>
        </dl>
        {currentTask.status === "open" ? (
          <button
            type="button"
            onClick={() => complete.mutate()}
            disabled={complete.isPending}
          >
            {complete.isPending ? "Completing…" : "Complete task"}
          </button>
        ) : (
          <p>Task is {currentTask.status}.</p>
        )}
        {complete.isError && (
          <p className={styles.error} role="alert">
            {complete.error instanceof ApiClientError
              ? complete.error.message
              : "Unable to complete task."}
          </p>
        )}
        {complete.isSuccess && <p role="status">Task completed.</p>}
      </div>
    </article>
  );
}

export default TaskList;
