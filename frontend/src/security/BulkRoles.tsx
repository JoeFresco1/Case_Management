import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import styles from "./BulkRoles.module.css";

type Role = components["schemas"]["RoleSummary"];
type Operation = "add" | "remove";
type Row = {
  userId: string;
  status: "Ready" | "Applying" | "Applied" | "Failed";
  error?: string;
};
type Preview = { role: Role; operation: Operation; rows: Row[] };

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Manage up to 50 independent role assignments for users in the current tenant. */
export function BulkRoles() {
  const roles = useQuery({ queryKey: ["roles"], queryFn: api.roles.list });
  const [roleId, setRoleId] = useState("");
  const [operation, setOperation] = useState<Operation>("add");
  const [source, setSource] = useState("");
  const [validation, setValidation] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const running = useRef(false);

  function prepare(event: FormEvent) {
    event.preventDefault();
    const role = roles.data?.find((entry) => entry.id === roleId);
    const ids = [
      ...new Set(
        source
          .split(/[,\r\n]+/)
          .map((id) => id.trim().toLowerCase())
          .filter(Boolean),
      ),
    ];
    if (!role) return setValidation("Select a role.");
    if (!ids.length) return setValidation("Enter at least one user ID.");
    if (ids.length > 50)
      return setValidation("Enter up to 50 unique user IDs per change.");
    const invalid = ids.find((id) => !uuid.test(id));
    if (invalid)
      return setValidation(
        `Invalid user ID: ${invalid}. Enter a UUID in 8-4-4-4-12 format.`,
      );
    setValidation("");
    setPreview({
      role,
      operation,
      rows: ids.map((userId) => ({ userId, status: "Ready" })),
    });
  }

  function updateRow(userId: string, status: Row["status"], error?: string) {
    setPreview(
      (current) =>
        current && {
          ...current,
          rows: current.rows.map((row) =>
            row.userId === userId ? { userId, status, error } : row,
          ),
        },
    );
  }

  async function apply() {
    if (!preview || running.current) return;
    // Lock synchronously so two clicks in one React update cannot start overlapping batches.
    running.current = true;
    setBusy(true);
    try {
      for (const row of preview.rows) {
        if (row.status !== "Ready" && row.status !== "Failed") continue;
        updateRow(row.userId, "Applying");
        try {
          if (preview.operation === "add")
            await api.roles.assign(preview.role.id, row.userId);
          else await api.roles.revokeAssignment(preview.role.id, row.userId);
          updateRow(row.userId, "Applied");
        } catch (error) {
          updateRow(
            row.userId,
            "Failed",
            error instanceof ApiClientError
              ? error.message
              : "Request failed. Check the connection and retry.",
          );
        }
      }
    } finally {
      running.current = false;
      setBusy(false);
    }
  }

  const failed =
    preview?.rows.filter((row) => row.status === "Failed").length ?? 0;
  const applied =
    preview?.rows.filter((row) => row.status === "Applied").length ?? 0;
  const finished =
    !!preview && !busy && applied + failed === preview.rows.length;

  return (
    <section className={styles.page}>
      <h1>Bulk role assignments</h1>
      {preview ? (
        <div className={styles.panel}>
          <header>
            <h2>
              {finished || busy ? "Assignment results" : "Review changes"}
            </h2>
            <p>
              {preview.operation === "add" ? "Assign" : "Remove"}{" "}
              <strong>{preview.role.name}</strong>{" "}
              {preview.operation === "add" ? "for" : "from"}{" "}
              {preview.rows.length}{" "}
              {preview.rows.length === 1 ? "user" : "users"}.
            </p>
          </header>
          <div className={styles.scroll}>
            <table>
              <thead>
                <tr>
                  <th scope="col">User ID</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row) => (
                  <tr key={row.userId}>
                    <td>
                      <code>{row.userId}</code>
                    </td>
                    <td>
                      <span className={styles.pill} data-state={row.status}>
                        {row.status}
                      </span>
                      {row.error && <small>{row.error}</small>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className={styles.footer}>
            {(busy || finished) && (
              <p role="status">
                {applied} applied · {failed} failed
              </p>
            )}
            {(!finished || failed > 0) && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void apply()}
              >
                {busy
                  ? "Applying…"
                  : finished
                    ? "Retry failed"
                    : "Apply changes"}
              </button>
            )}
            <button
              className={styles.secondary}
              type="button"
              disabled={busy}
              onClick={() => {
                if (finished) setSource("");
                setPreview(null);
              }}
            >
              {finished ? "Start another change" : "Edit changes"}
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={prepare} className={styles.panel}>
          <header>
            <h2>Assignment details</h2>
          </header>
          <div className={styles.inputs}>
            <label>
              Operation
              <select
                value={operation}
                onChange={(event) =>
                  setOperation(event.target.value as Operation)
                }
              >
                <option value="add">Assign role</option>
                <option value="remove">Remove role</option>
              </select>
            </label>
            <label>
              Role
              <select
                value={roleId}
                onChange={(event) => setRoleId(event.target.value)}
                disabled={!roles.data?.length}
              >
                <option value="">
                  {roles.isLoading ? "Loading roles…" : "Select a role"}
                </option>
                {roles.data?.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.users}>
              User IDs
              <textarea
                value={source}
                rows={6}
                aria-describedby="bulk-role-guidance"
                onChange={(event) => setSource(event.target.value)}
              />
            </label>
            <p className={styles.help} id="bulk-role-guidance">
              Enter up to 50 user UUIDs, separated by commas or new lines.
              Duplicate IDs count once.
            </p>
          </div>
          {roles.isError && (
            <p className={styles.error} role="alert">
              {roles.error instanceof ApiClientError
                ? roles.error.message
                : "Unable to load roles."}
            </p>
          )}
          {roles.isSuccess && !roles.data.length && (
            <p className={styles.help}>No roles are available.</p>
          )}
          {validation && (
            <p className={styles.error} role="alert">
              {validation}
            </p>
          )}
          <div className={styles.footer}>
            <button disabled={!roles.data?.length} type="submit">
              Preview changes
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

export default BulkRoles;
