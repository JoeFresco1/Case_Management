import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useState } from "react";

import { api, ApiClientError } from "../api/client";
import type { TaskView } from "../api/client";
import styles from "../cases/CaseWorkspace.module.css";

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
    queryKey: ["tasks", "list", { assignee: props.assignee ?? null, status, limit }],
    queryFn: () => api.listTasks({ assignee: props.assignee, status, limit }),
    staleTime: 15_000,
  });
  if (query.isLoading) return <p>Loading tasks…</p>;
  if (query.error) return <p className={styles.error}>{query.error instanceof ApiClientError ? query.error.message : "Failed to load tasks."}</p>;
  const tasks = query.data?.items ?? [];
  if (tasks.length === 0) return <p className={styles.empty}>No tasks match these filters.</p>;
  const now = new Date();
  return (
    <>
      <h1>Tasks</h1>
      <table className={styles.table}>
        <thead><tr><th>Title</th><th>Case</th><th>Status</th><th>Priority</th><th>Due</th><th>Assignee</th></tr></thead>
        <tbody>{tasks.map((task) => {
          const overdue = task.due_at ? new Date(task.due_at).getTime() < now.getTime() : false;
          return <tr key={task.id} className={overdue ? styles.errorRow : undefined}>
            <td><a href={`/tasks/${task.id}`}>{task.title}</a></td>
            <td><a href={`/cases/${task.case_id}`}><code>{task.case_id.slice(0, 8)}</code></a></td>
            <td>{task.status}</td><td>{task.priority}</td>
            <td className={overdue ? styles.error : undefined}>{task.due_at ? new Date(task.due_at).toLocaleString() : "—"}</td>
            <td>{task.assignee_user_id ? `@${task.assignee_user_id.slice(0, 8)}` : task.assignee_queue_id ? `queue:${task.assignee_queue_id}` : "—"}</td>
          </tr>;
        })}</tbody>
      </table>
    </>
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
    const task = detailQuery.data?.items.find((item) => item.id === taskId);
    return task ? <TaskDetail task={task} /> : <p className={styles.empty}>Task not found.</p>;
  }
  return <>
    <form aria-label="Task filters" onSubmit={(event) => event.preventDefault()}>
      <label>Assignee <select value={assignee} onChange={(event) => setAssignee(event.target.value as "me" | "all")}>
        <option value="me">My tasks</option><option value="all">All tasks</option>
      </select></label>{" "}
      <label>Status <select value={status} onChange={(event) => setStatus(event.target.value as "open" | "all")}>
        <option value="open">Open</option><option value="all">All statuses</option>
      </select></label>
    </form>
    <TaskList assignee={assignee === "me" ? "me" : undefined} statusFilter={status} />
  </>;
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
  return <article>
    <p><a href="/tasks">← Back to tasks</a></p>
    <h1>{currentTask.title}</h1>
    <dl><dt>Type</dt><dd>{currentTask.type}</dd><dt>Status</dt><dd>{currentTask.status}</dd><dt>Priority</dt><dd>{currentTask.priority}</dd><dt>Case</dt><dd><a href={`/cases/${currentTask.case_id}`}>{currentTask.case_id}</a></dd><dt>Due</dt><dd>{currentTask.due_at ? new Date(currentTask.due_at).toLocaleString() : "—"}</dd></dl>
    {currentTask.status === "open" ? <button type="button" onClick={() => complete.mutate()} disabled={complete.isPending}>{complete.isPending ? "Completing…" : "Complete task"}</button> : <p>Task is {currentTask.status}.</p>}
    {complete.isError && <p className={styles.error}>{complete.error instanceof ApiClientError ? complete.error.message : "Unable to complete task."}</p>}
    {complete.isSuccess && <p role="status">Task completed.</p>}
  </article>;
}

export default TaskList;
