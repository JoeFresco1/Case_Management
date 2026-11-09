import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  ApiClientError,
  CASES_PAGE_LIMIT_DEFAULT,
  CASES_PAGE_LIMIT_MAX,
} from "../api/client";

import { CasePreview, ColumnPicker, useRegisterColumns } from "./RegisterTools";
import tools from "./RegisterTools.module.css";
import styles from "./CaseWorkspace.module.css";

/**
 * Standalone case-list page (P01-US2 / FR-002).
 *
 * Kept as a named export so callers (router, header navigation) can
 * mount it without going through the workspace router. The list view
 * is a strict subset of the workspace summary — workspace-only slots
 * (assignee / queue / participants / etc.) are never sent over the
 * wire to keep the payload compact (see backend
 * `summary.summary_to_list_dict`).
 */
export function CaseList({
  limit: limitProp = CASES_PAGE_LIMIT_DEFAULT,
}: { limit?: number } = {}) {
  const limit = Math.min(limitProp, CASES_PAGE_LIMIT_MAX);
  const [page, setPage] = useState(0);
  const [preview, setPreview] = useState<string | null>(null);
  const layout = useRegisterColumns();
  const visible = (column: (typeof layout.columns)[number]) =>
    layout.columns.includes(column);
  const offset = page * limit;
  const listQuery = useQuery({
    queryKey: ["cases", "list", { limit, offset }],
    queryFn: () => api.listCases({ limit, offset }),
    staleTime: 30_000,
  });

  if (listQuery.isLoading) return <p>Loading cases…</p>;
  if (listQuery.error) {
    return (
      <p className={styles.error}>
        {listQuery.error instanceof ApiClientError
          ? listQuery.error.message
          : "Failed to load cases."}
      </p>
    );
  }
  const cases = listQuery.data?.items ?? [];
  // why: server returns no total count, so a full page implies a next page may exist.
  const hasNextPage = cases.length === limit;
  const hasPrevPage = page > 0;
  const isNavigating = listQuery.isLoading || listQuery.isFetching;
  if (cases.length === 0) {
    return (
      <>
        <p className={styles.empty}>
          No cases yet. <a href="/cases/new">Create a case</a> to get started.
        </p>
        {hasPrevPage ? (
          <nav className={styles.pagination} aria-label="Cases pagination">
            <button
              type="button"
              onClick={() => {
                setPreview(null);
                setPage((p) => Math.max(0, p - 1));
              }}
              disabled={isNavigating}
            >
              Previous
            </button>
          </nav>
        ) : null}
      </>
    );
  }
  return (
    <>
      <header className={styles.pageHeading}>
        <h1>Cases</h1>
        <a href="/search">Search cases →</a>
      </header>
      <div className={tools.tools}>
        <ColumnPicker {...layout} />
      </div>
      <div className={preview ? tools.split : undefined}>
        <div className={styles.register}>
          <table>
            <thead>
              <tr>
                <th>Case</th>
                {visible("status") && <th>Status</th>}
                {visible("priority") && <th>Priority</th>}
                {visible("stage") && <th>Workflow stage</th>}
                {visible("updated") && <th>Updated</th>}
                <th aria-label="Case actions" />
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr
                  key={c.id}
                  className={preview === c.id ? tools.selected : undefined}
                >
                  <td>
                    <a href={`/cases/${c.id}`}>{c.title}</a>
                    <small>{c.reference}</small>
                  </td>
                  {visible("status") && (
                    <td>
                      <span className={styles.pill}>{c.status}</span>
                    </td>
                  )}
                  {visible("priority") && <td>{c.priority}</td>}
                  {visible("stage") && (
                    <td>
                      {c.current_workflow_state?.split("_").join(" ") ?? "—"}
                    </td>
                  )}
                  {visible("updated") && (
                    <td>{new Date(c.updated_at).toLocaleDateString()}</td>
                  )}
                  <td>
                    <button
                      className={tools.previewButton}
                      onClick={() => setPreview(preview === c.id ? null : c.id)}
                      aria-pressed={preview === c.id}
                      aria-label={"Preview " + c.title}
                    >
                      Preview
                    </button>
                    <a href={"/records/log?case="+c.id} aria-label={"Activity history for "+c.title}>History</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {preview && (
          <CasePreview caseId={preview} close={() => setPreview(null)} />
        )}
      </div>
      <nav className={styles.pagination} aria-label="Cases pagination">
        <span aria-live="polite">Page {page + 1}</span>{" "}
        <button
          type="button"
          onClick={() => {
            setPreview(null);
            setPage((p) => Math.max(0, p - 1));
          }}
          disabled={!hasPrevPage || isNavigating}
        >
          Previous
        </button>{" "}
        <button
          type="button"
          onClick={() => {
            setPreview(null);
            setPage((p) => p + 1);
          }}
          disabled={!hasNextPage || isNavigating}
        >
          Next
        </button>
      </nav>
    </>
  );
}
