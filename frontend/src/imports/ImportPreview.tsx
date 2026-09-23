import { useState } from "react";

import {
  ApiClientError,
  api,
  type CommitResponse,
  type ImportJob,
  type ImportRowsPayload,
  type MappingVersionRequest,
} from "../api/client";
import type { components } from "../api/types.gen";
import styles from "../cases/CaseWorkspace.module.css";

/**
 * Operator preview surface for the import pipeline (P14-US2 / FM-026 /
 * P04-US3, D-6). Submits the synthetic import package to
 * ``POST /api/v1/imports/dry-run`` and shows the per-row reconciliation
 * before offering the commit step.
 *
 * The synthetic dataset is intentionally tiny (two valid rows + one
 * invalid row from example.test addresses) so the UI exercises the
 * preview / commit flow end-to-end without pulling from an enterprise
 * system. Live enterprise connectors land behind P14 connector
 * authorization (Pillar P14 §2).
 *
 * Wire discipline: every shape below is an alias of the generated
 * OpenAPI contract (``types.gen.ts``) reached through the shared
 * ``api.imports`` client. The component declares no local
 * ``interface`` for the wire — drift surfaces at typecheck time.
 * Row *payload bytes* (``values``) are editable inputs only; they are
 * never rendered into the provenance or reconciliation readouts
 * (Constitution V).
 */

// why alias, not interface: the OpenAPI generator owns the field list;
// a local interface would silently diverge when the backend adds a field.
type ImportRowInput = components["schemas"]["ImportRowRequest"];

const SYNTHETIC_MAPPING: MappingVersionRequest = {
  version: 1,
  case_type_key: "ServiceRequest",
  fields: [
    { source: "request_id", target: "requestId" },
    { source: "title", target: "title" },
    { source: "summary", target: "summary" },
    { source: "priority", target: "priority" },
  ],
  title_field: "title",
  description_field: "summary",
  priority_field: "priority",
  documents: null,
};

const SYNTHETIC_ROWS: ImportRowInput[] = [
  {
    index: 1,
    values: {
      request_id: "SR-0001",
      title: "Onboarding help",
      summary: "Need a walkthrough of the workspace.",
      priority: "normal",
      contact_email: "intake@example.test",
    },
  },
  {
    index: 2,
    values: {
      request_id: "SR-0002",
      title: "Document upload",
      summary: "Attachment failed mid-upload.",
      priority: "high",
      contact_email: "reviewer@example.test",
    },
  },
  // Invalid row: priority outside the enum; intentionally included
  // so the reconciliation reports "1 invalid" in the dry-run.
  {
    index: 3,
    values: {
      request_id: "SR-0003",
      title: "",
      summary: "Priority below is not in the allowed set.",
      priority: "yesterday",
      contact_email: "noise@example.test",
    },
  },
];

/** Editable columns of the synthetic package (subset of row values). */
const ROW_FIELDS = [
  "request_id",
  "title",
  "summary",
  "priority",
  "contact_email",
] as const;

/**
 * Render an API failure as operator text.
 *
 * The server error contract is safe-only (code/message/details, never
 * SQL or secrets), so echoing ``details`` is allowed and is what lets
 * the operator fix the package without losing their place.
 */
function toErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError) {
    const detail =
      err.details != null && Object.keys(err.details).length > 0
        ? ` — ${JSON.stringify(err.details)}`
        : "";
    return `${err.message}${detail}`;
  }
  return err instanceof Error ? err.message : fallback;
}

/**
 * Summarize the opaque mapping snapshot for the provenance line.
 *
 * ``ImportJobResponse.mapping`` is a free-form dict (inline mapping or
 * registry-resolved snapshot); only the stable ``case_type_key`` and
 * ``version`` are shown so row payloads never leak into the readout.
 */
function mappingLabel(mapping: Record<string, unknown>): string {
  const rawVersion = mapping["version"];
  const rawKey = mapping["case_type_key"];
  const version = typeof rawVersion === "number" ? rawVersion : "?";
  const key = typeof rawKey === "string" ? rawKey : "unknown mapping";
  return `${key} v${String(version)}`;
}

export function ImportPreview() {
  // why state (not constants): validation errors must preserve what the
  // operator typed, so the package lives in state and error paths never
  // reset it.
  const [sourceLabel, setSourceLabel] = useState("synthetic-fixture");
  const [sourceFormat, setSourceFormat] = useState("csv");
  const [mappingId, setMappingId] = useState("");
  const [rows, setRows] = useState<ImportRowInput[]>(SYNTHETIC_ROWS);
  const [preview, setPreview] = useState<ImportJob | null>(null);
  const [committed, setCommitted] = useState<CommitResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function buildPayload(): ImportRowsPayload {
    const trimmedMappingId = mappingId.trim();
    // why branch: the registry path (FM-026) references a published
    // ImportMapping by id instead of sending the inline document; the
    // backend resolves and snapshots it onto the job.
    if (trimmedMappingId !== "") {
      return {
        source_label: sourceLabel,
        source_format: sourceFormat,
        mapping_id: trimmedMappingId,
        rows,
      };
    }
    return {
      source_label: sourceLabel,
      source_format: sourceFormat,
      mapping: SYNTHETIC_MAPPING,
      rows,
    };
  }

  function updateRowValue(rowIndex: number, field: string, value: string) {
    setRows((current) =>
      current.map((row) =>
        row.index === rowIndex
          ? { ...row, values: { ...row.values, [field]: value } }
          : row,
      ),
    );
  }

  async function runPreview() {
    setError(null);
    setBusy(true);
    try {
      const result = await api.imports.dryRun(buildPayload());
      setPreview(result.job);
      setCommitted(null);
    } catch (err) {
      // why no setRows here: entered data must survive a 422/400 so the
      // operator can fix the flagged row instead of retyping the package.
      setError(toErrorMessage(err, "preview failed"));
    } finally {
      setBusy(false);
    }
  }

  async function runCommit() {
    setError(null);
    setBusy(true);
    try {
      const result = await api.imports.commit(buildPayload());
      setCommitted(result);
      setPreview(result.job);
    } catch (err) {
      // why no setRows here: same preservation rule as runPreview.
      setError(toErrorMessage(err, "commit failed"));
    } finally {
      setBusy(false);
    }
  }

  async function refreshJob() {
    if (preview === null) {
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const job = await api.imports.get(preview.id);
      setPreview(job);
    } catch (err) {
      setError(toErrorMessage(err, "refresh failed"));
    } finally {
      setBusy(false);
    }
  }

  const activeJob = committed?.job ?? preview;
  const reconciliation = activeJob?.reconciliation ?? null;

  return (
    <section>
      <h2>Import preview (P14-US2 / FM-026)</h2>
      <p className={styles.meta}>
        Synthetic fixture only — <code>example.test</code> addresses. Live
        enterprise connectors require explicit authorization (Pillar P14
        §2).
      </p>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <label>
          Source label{" "}
          <input
            type="text"
            aria-label="Source label"
            value={sourceLabel}
            onChange={(event) => setSourceLabel(event.target.value)}
            maxLength={200}
          />
        </label>
        <label>
          Source format{" "}
          <input
            type="text"
            aria-label="Source format"
            value={sourceFormat}
            onChange={(event) => setSourceFormat(event.target.value)}
            maxLength={16}
          />
        </label>
        <label>
          Mapping id (registry, optional){" "}
          <input
            type="text"
            aria-label="Mapping id"
            placeholder="published ImportMapping id"
            value={mappingId}
            onChange={(event) => setMappingId(event.target.value)}
          />
        </label>
      </div>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Row</th>
            {ROW_FIELDS.map((field) => (
              <th key={field}>{field}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.index}>
              <td>{row.index}</td>
              {ROW_FIELDS.map((field) => (
                <td key={field}>
                  <input
                    type="text"
                    aria-label={`Row ${row.index} ${field}`}
                    value={row.values[field] ?? ""}
                    onChange={(event) =>
                      updateRowValue(row.index, field, event.target.value)
                    }
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ display: "flex", gap: "0.5rem" }}>
        <button type="button" disabled={busy} onClick={runPreview}>
          {busy ? "Running…" : "Dry-run"}
        </button>
        <button
          type="button"
          disabled={busy || !preview}
          onClick={runCommit}
        >
          {busy ? "Committing…" : "Commit accepted rows"}
        </button>
        {preview ? (
          <button type="button" disabled={busy} onClick={refreshJob}>
            Refresh job
          </button>
        ) : null}
      </div>
      {error ? <p className={styles.error}>{error}</p> : null}
      {reconciliation ? (
        <p className={styles.meta}>
          Reconciliation:&nbsp;
          <strong>{reconciliation.committed_count}</strong> accepted,&nbsp;
          <strong className={styles.error}>
            {reconciliation.rejected_count}
          </strong>{" "}
          rejected,&nbsp;
          <strong>{reconciliation.skipped_count}</strong> skipped
        </p>
      ) : null}
      {activeJob ? (
        <p className={styles.meta}>
          Provenance: source <code>{activeJob.source_label}</code> (
          {activeJob.source_format}) · mapping{" "}
          <code>{mappingLabel(activeJob.mapping)}</code> · job{" "}
          <code>{activeJob.id.slice(0, 8)}</code> status{" "}
          <strong>{activeJob.status}</strong> · correlation{" "}
          <code>{activeJob.correlation_id}</code>
        </p>
      ) : null}
      {activeJob ? (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Row</th>
              <th>Outcome</th>
              <th>Source ID</th>
              <th>Case</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {activeJob.results.map((row) => (
              <tr key={row.row_index}>
                <td>{row.row_index}</td>
                <td>
                  <code>{row.outcome}</code>
                </td>
                <td>{row.source_id ?? "—"}</td>
                <td>
                  {row.case_id ? (
                    <code>{row.case_id.slice(0, 8)}</code>
                  ) : (
                    "—"
                  )}
                </td>
                <td>
                  {row.error_code ? (
                    <span className={styles.error}>
                      {row.error_code}: {row.error_message}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {committed ? (
        <p className={styles.meta}>
          Job <code>{committed.job.id.slice(0, 8)}</code> status{" "}
          <strong>{committed.job.status}</strong>; created{" "}
          <strong>{committed.created_case_ids.length}</strong> case(s)
          {committed.created_case_ids.length > 0 ? (
            <>
              :{" "}
              <code>{committed.created_case_ids.join(", ")}</code>
            </>
          ) : (
            "."
          )}
        </p>
      ) : null}
    </section>
  );
}

export default ImportPreview;
