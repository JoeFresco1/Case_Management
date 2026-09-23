import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, ApiClientError, CASES_PAGE_LIMIT_DEFAULT, CASES_PAGE_LIMIT_MAX } from "../api/client";

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
export function CaseList({ limit: limitProp = CASES_PAGE_LIMIT_DEFAULT }: { limit?: number } = {}) {
  const limit = Math.min(limitProp, CASES_PAGE_LIMIT_MAX);
  const [page, setPage] = useState(0);
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
          No cases yet. The seed script should have created one. Run
          <code> uv run python -m app.cli seed</code>.
        </p>
        {hasPrevPage ? (
          <nav aria-label="Cases pagination">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
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
      <ul className={styles.list}>
        {cases.map((c) => (
          <li key={c.id} className={styles.listItem}>
            <a href={`/cases/${c.id}`}>
              <strong>{c.reference}</strong> — {c.title}
            </a>
            <div className={styles.meta}>
              <span>{c.status}</span> · <span>{c.priority}</span>
            </div>
          </li>
        ))}
      </ul>
      <nav aria-label="Cases pagination">
        <span aria-live="polite">Page {page + 1}</span>{" "}
        <button
          type="button"
          onClick={() => setPage((p) => Math.max(0, p - 1))}
          disabled={!hasPrevPage || isNavigating}
        >
          Previous
        </button>{" "}
        <button
          type="button"
          onClick={() => setPage((p) => p + 1)}
          disabled={!hasNextPage || isNavigating}
        >
          Next
        </button>
      </nav>
    </>
  );
}