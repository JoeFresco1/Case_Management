import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError, api } from "../api/client";

import styles from "./PublicationHistory.module.css";

/**
 * Configuration publication history + second-admin approval (FM-021).
 *
 * Renders the tenant-wide governance timeline newest-first (author,
 * action, effective change summary, approval state) and exposes the
 * record + approve flows. When the tenant policy mandates
 * second-admin approval, the record response reports
 * `approval_required` and the entry stays pending until a *different*
 * administrator approves — the UI surfaces that state explicitly so
 * separation of duties is visible, not implied.
 */

function errorMessage(error: unknown): string {
  if (error instanceof ApiClientError) return error.message;
  return "Publication operation failed.";
}

export function PublicationHistory() {
  const queryClient = useQueryClient();
  const [artifactType, setArtifactType] = useState("");
  const [artifactId, setArtifactId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [action, setAction] = useState("publish");
  const [changeSummary, setChangeSummary] = useState("");
  const [approverId, setApproverId] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const historyQuery = useQuery({
    queryKey: ["publications"],
    queryFn: () => api.publications.list(),
  });

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["publications"] });

  const record = useMutation({
    mutationFn: () =>
      api.publications.record({
        artifact_type: artifactType.trim(),
        artifact_id: artifactId.trim(),
        version_id: versionId.trim(),
        action: action as "draft" | "validate" | "publish" | "retire",
        change_summary: changeSummary.trim(),
      }),
    onSuccess: (response) => {
      setError(null);
      setNotice(
        response.approval_required
          ? `Recorded ${response.publication_id}. Second-admin approval required before it takes effect.`
          : `Recorded ${response.publication_id}. No further approval required.`,
      );
      void refresh();
    },
    onError: (e: unknown) => {
      setNotice(null);
      setError(errorMessage(e));
    },
  });

  const approve = useMutation({
    mutationFn: () =>
      api.publications.approve({
        artifact_type: artifactType.trim(),
        artifact_id: artifactId.trim(),
        version_id: versionId.trim(),
        approving_actor_id: approverId.trim(),
        change_summary: changeSummary.trim(),
      }),
    onSuccess: (response) => {
      setError(null);
      setNotice(`Approved ${response.publication_id}.`);
      void refresh();
    },
    onError: (e: unknown) => {
      setNotice(null);
      setError(errorMessage(e));
    },
  });

  const formValid =
    artifactType.trim().length > 0 &&
    artifactId.trim().length > 0 &&
    versionId.trim().length > 0;

  return (
    <section className={styles.console} aria-label="Publication history">
      <div className={styles.header}>
        <h2>Publication history</h2>
        <p>
          Authorship, approvals, and change summaries for every published
          configuration artifact, newest first.
        </p>
      </div>
      {historyQuery.isLoading && <p>Loading history…</p>}
      {historyQuery.error && (
        <p className={styles.error} role="alert">
          {errorMessage(historyQuery.error)}
        </p>
      )}
      <ul className={styles.timeline}>
        {(historyQuery.data?.items ?? []).map((record) => (
          <li key={record.id} className={styles.record}>
            <div className={styles.recordHeader}>
              <strong>{record.artifact_type}</strong>
              <span className={styles.action}>{record.action}</span>
            </div>
            <div className={styles.meta}>
              <span title={record.actor_id}>actor {record.actor_id.slice(0, 8)}…</span> ·{" "}
              <span>{record.occurred_at}</span>
            </div>
            {record.change_summary && (
              <p className={styles.summary}>{record.change_summary}</p>
            )}
          </li>
        ))}
      </ul>
      {!historyQuery.isLoading && (historyQuery.data?.items ?? []).length === 0 && (
        <p className={styles.empty}>No publications recorded for this tenant.</p>
      )}
      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          record.mutate();
        }}
      >
        <h3>Record a publication</h3>
        <label>
          Artifact type
          <input
            value={artifactType}
            onChange={(event) => setArtifactType(event.target.value)}
            placeholder="CaseTypeVersion"
            required
          />
        </label>
        <label>
          Artifact id
          <input
            value={artifactId}
            onChange={(event) => setArtifactId(event.target.value)}
            placeholder="artifact uuid"
            required
          />
        </label>
        <label>
          Version id
          <input
            value={versionId}
            onChange={(event) => setVersionId(event.target.value)}
            placeholder="version uuid"
            required
          />
        </label>
        <label>
          Action
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="draft">draft</option>
            <option value="validate">validate</option>
            <option value="publish">publish</option>
            <option value="retire">retire</option>
          </select>
        </label>
        <label>
          Change summary
          <input
            value={changeSummary}
            onChange={(event) => setChangeSummary(event.target.value)}
          />
        </label>
        <button type="submit" disabled={!formValid || record.isPending}>
          {record.isPending ? "Recording…" : "Record publication"}
        </button>
      </form>
      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          approve.mutate();
        }}
      >
        <h3>Second-admin approval</h3>
        <p className={styles.hint}>
          The approver must differ from the publishing actor — the server
          enforces separation of duties.
        </p>
        <label>
          Approving actor id
          <input
            value={approverId}
            onChange={(event) => setApproverId(event.target.value)}
            placeholder="admin uuid"
            required
          />
        </label>
        <button
          type="submit"
          disabled={!formValid || !approverId.trim() || approve.isPending}
        >
          {approve.isPending ? "Approving…" : "Approve publication"}
        </button>
      </form>
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
