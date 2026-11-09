import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiClientError } from "../api/client";
import { Icon } from "../shell/Icon";
import styles from "./RegisterTools.module.css";

const names = {
  status: "Status",
  priority: "Priority",
  stage: "Workflow stage",
  updated: "Updated",
};
export type RegisterColumn = keyof typeof names;
const defaults: RegisterColumn[] = ["status", "priority", "stage"];
const storageKey = "case-register:columns:v1";
function readColumns(): RegisterColumn[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(storageKey) ?? "null");
    return Array.isArray(raw)
      ? (Object.keys(names).filter((k) => raw.includes(k)) as RegisterColumn[])
      : defaults;
  } catch {
    return defaults;
  }
}

/** Store display preferences only. Case data is always obtained from the API. */
export function useRegisterColumns() {
  const [columns, setColumns] = useState(readColumns);
  function toggle(column: RegisterColumn) {
    const next = columns.includes(column)
      ? columns.filter((k) => k !== column)
      : [...columns, column];
    setColumns(next);
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* Private browsing can disable storage. */
    }
  }
  return { columns, toggle };
}

/** Configure the columns shown in the case register. */
export function ColumnPicker({
  columns,
  toggle,
}: ReturnType<typeof useRegisterColumns>) {
  return (
    <details className={styles.columns}>
      <summary>
        <Icon name="settings" /> Columns
      </summary>
      <fieldset>
        <legend>Visible columns</legend>
        {(Object.keys(names) as RegisterColumn[]).map((key) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={columns.includes(key)}
              onChange={() => toggle(key)}
            />
            {names[key]}
          </label>
        ))}
      </fieldset>
    </details>
  );
}

/** Read the selected case alongside its register, retaining the current page. */
export function CasePreview({
  caseId,
  close,
}: {
  caseId: string;
  close: () => void;
}) {
  const query = useQuery({
    queryKey: ["cases", caseId],
    queryFn: () => api.getCase(caseId),
  });
  const c = query.data;
  return (
    <aside className={styles.preview} aria-label="Case preview">
      <header>
        <h2>Case preview</h2>
        <button type="button" aria-label="Close preview" onClick={close}>
          ×
        </button>
      </header>
      {query.isLoading && <p>Loading case…</p>}
      {query.error && (
        <p role="alert">
          {query.error instanceof ApiClientError
            ? query.error.message
            : "Unable to load case."}
        </p>
      )}
      {c && (
        <>
          <small>{c.reference}</small>
          <h3>{c.title}</h3>
          <p>{c.description || "No description recorded."}</p>
          <dl>
            <dt>Priority</dt>
            <dd>{c.priority}</dd>
            <dt>Workflow</dt>
            <dd>
              {c.current_workflow_state?.split("_").join(" ") ?? "Unassigned"}
            </dd>
          </dl>
          <a href={`/cases/${c.id}`}>Open case file →</a>
          <a href={`/cases/${c.id}?view=documents`}>Review documents →</a>
        </>
      )}
    </aside>
  );
}
