import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiClientError } from "../api/client";
import { request, requestVoid } from "../api/client/http";
import type { components } from "../api/types.gen";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Queue administration console (P02-US3, US9 / FM-016).
 *
 * Renders a CRUD surface over ``/api/v1/admin/queues``:
 *
 * - list existing queues (key, policy, member count, cursor)
 * - create a new queue with key / name / policy / members
 * - reconfigure an existing queue (rename, swap policy, replace
 *   members, change capacity)
 *
 * The component is intentionally read-write on a single page so the
 * process admin does not have to navigate away mid-edit. Form state
 * is local; failures are surfaced inline.
 */
export type QueueSummary = components["schemas"]["QueueSummary"];
export type DefineQueueRequest = components["schemas"]["DefineQueueRequest"];
type ReconfigureQueueRequest = components["schemas"]["ReconfigureQueueRequest"];

async function fetchQueues(): Promise<QueueSummary[]> {
  return request<QueueSummary[]>("/admin/queues");
}

async function defineQueue(body: DefineQueueRequest): Promise<QueueSummary> {
  return request<QueueSummary>("/admin/queues", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

async function reconfigureQueue(
  queueId: string,
  body: ReconfigureQueueRequest,
): Promise<QueueSummary> {
  return request<QueueSummary>(`/admin/queues/${queueId}`, {
    method: "PUT",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

async function retireQueue(queueId: string): Promise<void> {
  await requestVoid(`/admin/queues/${queueId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

export function QueueAdmin() {
  const queryClient = useQueryClient();
  const queuesQuery = useQuery({
    queryKey: ["admin", "queues"],
    queryFn: fetchQueues,
    staleTime: 30_000,
  });

  const define = useMutation({
    mutationFn: defineQueue,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "queues"] });
    },
  });

  const reconfigure = useMutation({
    mutationFn: (vars: { id: string; body: ReconfigureQueueRequest }) =>
      reconfigureQueue(vars.id, vars.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "queues"] });
    },
  });

  const retire = useMutation({
    mutationFn: retireQueue,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "queues"] });
    },
  });

  if (queuesQuery.isLoading) return <p>Loading queues…</p>;
  if (queuesQuery.error) {
    const message =
      queuesQuery.error instanceof ApiClientError
        ? queuesQuery.error.message
        : "Failed to load queues.";
    return <p className={styles.error}>{message}</p>;
  }

  const queues = queuesQuery.data ?? [];

  return (
    <section>
      <h2>Queues</h2>
      <p className={styles.meta}>
        Configure the named work containers referenced by workflow
        transitions. A queue with <code>manual</code> policy hands
        tasks to whoever claims them; <code>round_robin</code> cycles
        through the member list in declared order.
      </p>

      <h3>Define new queue</h3>
      <DefineForm
        pending={define.isPending}
        error={define.error}
        onSubmit={(values) => define.mutate(values)}
      />

      <h3>Configured queues</h3>
      {queues.length === 0 ? (
        <p className={styles.empty}>No queues yet.</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Policy</th>
              <th>Capacity</th>
              <th>Members</th>
              <th>Cursor</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {queues.map((q) => (
              <tr key={q.id}>
                <td>
                  <code>{q.key}</code>
                </td>
                <td>{q.name}</td>
                <td>{q.assignment_policy}</td>
                <td>{q.capacity}</td>
                <td>{q.member_user_ids.length}</td>
                <td>{q.rr_cursor}</td>
                <td>
                  <button
                    type="button"
                    disabled={retire.isPending}
                    onClick={() => {
                      if (
                        confirm(
                          `Retire queue ${q.key}? Open tasks on the queue become unassigned.`,
                        )
                      ) {
                        retire.mutate(q.id);
                      }
                    }}
                  >
                    Retire
                  </button>
                  <button
                    type="button"
                    disabled={reconfigure.isPending}
                    onClick={() => {
                      const next = prompt(
                        "New policy? (manual|round_robin)",
                        q.assignment_policy,
                      );
                      if (
                        next === "manual" ||
                        next === "round_robin"
                      ) {
                        reconfigure.mutate({
                          id: q.id,
                          body: { assignment_policy: next },
                        });
                      }
                    }}
                  >
                    Switch policy
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function DefineForm(props: {
  pending: boolean;
  error: unknown;
  onSubmit: (values: DefineQueueRequest) => void;
}) {
  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const data = new FormData(form);
        const members = String(data.get("member_user_ids") ?? "")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        const key = String(data.get("key") ?? "").trim();
        const name = String(data.get("name") ?? "").trim();
        if (!key || !name) return;
        props.onSubmit({
          key,
          name,
          description: String(data.get("description") ?? ""),
          assignment_policy:
            data.get("policy") === "round_robin" ? "round_robin" : "manual",
          capacity: Number(data.get("capacity") ?? 0),
          member_user_ids: members,
        });
        form.reset();
      }}
    >
      <label>
        Key
        <input
          name="key"
          pattern="[a-z0-9][a-z0-9_\-]{0,63}"
          required
          placeholder="review_queue"
        />
      </label>
      <label>
        Name
        <input name="name" required placeholder="Review queue" />
      </label>
      <label>
        Description
        <input name="description" placeholder="optional" />
      </label>
      <label>
        Policy
        <select name="policy" defaultValue="manual">
          <option value="manual">manual</option>
          <option value="round_robin">round_robin</option>
        </select>
      </label>
      <label>
        Capacity
        <input
          name="capacity"
          type="number"
          min={0}
          defaultValue={0}
        />
      </label>
      <label>
        Members (comma-separated UUIDs)
        <input
          name="member_user_ids"
          placeholder="uuid1, uuid2"
        />
      </label>
      <button type="submit" disabled={props.pending}>
        {props.pending ? "Saving…" : "Define queue"}
      </button>
      {props.error instanceof ApiClientError ? (
        <p className={styles.error}>{props.error.message}</p>
      ) : null}
    </form>
  );
}

export default QueueAdmin;
