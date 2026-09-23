import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError, api, type RoleSummary } from "../api/client";

import styles from "./RoleAdmin.module.css";

/**
 * Role + permission administration console (P11-US4 / FM-048).
 *
 * Covers the full `POST/GET /api/v1/admin/roles` surface: define a
 * role, replace or grant/revoke permissions, assign/revoke user
 * assignments, and delete non-reserved roles. Every mutation goes
 * through the generated API contract; revocation is immediate
 * server-side (the policy reads assignments on each evaluation).
 * Reserved roles cannot be deleted — the server rejects with a
 * structured error surfaced inline.
 */

function errorMessage(error: unknown): string {
  if (error instanceof ApiClientError) return error.message;
  return "Role operation failed.";
}

function RoleCard({ role }: { role: RoleSummary }) {
  const queryClient = useQueryClient();
  const [permission, setPermission] = useState("");
  const [userId, setUserId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["roles"] });

  const grant = useMutation({
    mutationFn: () => api.roles.grantPermission(role.id, permission.trim()),
    onSuccess: () => {
      setPermission("");
      setError(null);
      void refresh();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const revokePermission = (name: string) =>
    api.roles
      .revokePermission(role.id, name)
      .then(() => {
        setError(null);
        void refresh();
      })
      .catch((e: unknown) => setError(errorMessage(e)));

  const assign = useMutation({
    mutationFn: () => api.roles.assign(role.id, userId.trim()),
    onSuccess: () => {
      setUserId("");
      setError(null);
      void refresh();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const revokeAssignment = (assignedUserId: string) =>
    api.roles
      .revokeAssignment(role.id, assignedUserId)
      .then(() => {
        setError(null);
        void refresh();
      })
      .catch((e: unknown) => setError(errorMessage(e)));

  const remove = useMutation({
    mutationFn: () => api.roles.deleteRole(role.id),
    onSuccess: () => {
      setError(null);
      void refresh();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  return (
    <li className={styles.roleCard}>
      <div className={styles.roleHeader}>
        <strong>{role.name}</strong>
        {role.is_reserved && <span className={styles.reserved}>reserved</span>}
      </div>
      {role.description && <p className={styles.description}>{role.description}</p>}
      <ul className={styles.permissions}>
        {role.permissions.map((name) => (
          <li key={name}>
            <code>{name}</code>{" "}
            <button type="button" onClick={() => void revokePermission(name)}>
              revoke
            </button>
          </li>
        ))}
        {role.permissions.length === 0 && <li className={styles.empty}>No permissions.</li>}
      </ul>
      <div className={styles.inlineForm}>
        <label>
          Permission
          <input
            value={permission}
            onChange={(event) => setPermission(event.target.value)}
            placeholder="CASE_READ"
          />
        </label>
        <button
          type="button"
          disabled={!permission.trim() || grant.isPending}
          onClick={() => grant.mutate()}
        >
          {grant.isPending ? "Granting…" : "Grant"}
        </button>
      </div>
      <div className={styles.inlineForm}>
        <label>
          User id
          <input
            value={userId}
            onChange={(event) => setUserId(event.target.value)}
            placeholder="user uuid"
          />
        </label>
        <button
          type="button"
          disabled={!userId.trim() || assign.isPending}
          onClick={() => assign.mutate()}
        >
          {assign.isPending ? "Assigning…" : "Assign"}
        </button>
      </div>
      {!role.is_reserved && (
        <button
          type="button"
          className={styles.danger}
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete role
        </button>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}{" "}
          {error.includes("assign") && userId && (
            <button type="button" onClick={() => void revokeAssignment(userId)}>
              revoke instead
            </button>
          )}
        </p>
      )}
    </li>
  );
}

export function RoleAdmin() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [permissions, setPermissions] = useState("");
  const [error, setError] = useState<string | null>(null);

  const rolesQuery = useQuery({ queryKey: ["roles"], queryFn: () => api.roles.list() });

  const define = useMutation({
    mutationFn: () =>
      api.roles.define({
        name: name.trim(),
        description: description.trim(),
        permissions: permissions
          .split(",")
          .map((p) => p.trim())
          .filter((p) => p.length > 0),
      }),
    onSuccess: () => {
      setName("");
      setDescription("");
      setPermissions("");
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["roles"] });
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  return (
    <section className={styles.console} aria-label="Role administration">
      <div className={styles.header}>
        <h2>Roles</h2>
        <p>
          Define roles, manage permissions, and assign users. Revocation is
          immediate — the policy reads assignments on every evaluation.
        </p>
      </div>
      {rolesQuery.isLoading && <p>Loading roles…</p>}
      {rolesQuery.error && (
        <p className={styles.error} role="alert">
          {errorMessage(rolesQuery.error)}
        </p>
      )}
      <ul className={styles.roles}>
        {(rolesQuery.data ?? []).map((role) => (
          <RoleCard key={role.id} role={role} />
        ))}
      </ul>
      {!rolesQuery.isLoading && (rolesQuery.data ?? []).length === 0 && (
        <p className={styles.empty}>No roles defined for this tenant.</p>
      )}
      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          define.mutate();
        }}
      >
        <h3>Define a role</h3>
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} required />
        </label>
        <label>
          Description
          <input value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>
        <label>
          Permissions (comma-separated)
          <input
            value={permissions}
            onChange={(event) => setPermissions(event.target.value)}
            placeholder="CASE_READ, CASE_WRITE"
          />
        </label>
        <button type="submit" disabled={!name.trim() || define.isPending}>
          {define.isPending ? "Defining…" : "Define role"}
        </button>
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </form>
    </section>
  );
}
