import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiClientError } from "../api/client";
import type {
  CaseSearchResponse,
  SearchFilterRequest,
} from "../api/types";

import styles from "./SavedSearches.module.css";

/**
 * Saved-searches pane (P06-US3, US5 / FM-032).
 *
 * The pane renders the actor's saved searches, lets them re-execute a
 * saved query (the run result is the same ``CaseSearchResponse`` shape
 * the live search page returns), and lets them delete the rows they no
 * longer need. The component is intentionally minimal: it owns the
 * "saved-searches" cache key and delegates all wire work to
 * ``api.*`` so the saved-searches endpoint and the search endpoint
 * can evolve independently.
 */
export function SavedSearches() {
  const queryClient = useQueryClient();
  const [lastResult, setLastResult] = useState<CaseSearchResponse | null>(null);

  const list = useQuery({
    queryKey: ["saved-searches"],
    queryFn: () => api.listSavedSearches({ limit: 50 }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteSavedSearch(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["saved-searches"] });
    },
  });

  const run = useMutation({
    mutationFn: (id: string) => api.runSavedSearch(id, { limit: 50, offset: 0 }),
    onSuccess: (response) => {
      setLastResult(response);
    },
  });

  if (list.isLoading) {
    return <p className={styles.empty}>Loading saved searches…</p>;
  }
  if (list.error instanceof ApiClientError) {
    return <p className={styles.error}>{list.error.message}</p>;
  }

  const items = list.data?.items ?? [];

  return (
    <section className={styles.section}>
      <h2 className={styles.title}>My saved searches</h2>
      {items.length === 0 ? (
        <p className={styles.empty}>
          No saved searches yet. Use the search page to save a query.
        </p>
      ) : (
        <ul className={styles.list}>
          {items.map((s) => (
            <li key={s.id} className={styles.item}>
              <div className={styles.summary}>
                <strong>{s.name}</strong>
                <span className={styles.meta}>
                  {summarizeShape(s.query, s.filters, s.sort)}
                </span>
              </div>
              <div className={styles.actions}>
                <button
                  type="button"
                  className={styles.button}
                  onClick={() => run.mutate(s.id)}
                  disabled={run.isPending}
                >
                  Run
                </button>
                <button
                  type="button"
                  className={styles.buttonDanger}
                  onClick={() => remove.mutate(s.id)}
                  disabled={remove.isPending}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {run.error instanceof ApiClientError ? (
        <p className={styles.error}>{run.error.message}</p>
      ) : null}

      {lastResult ? (
        <section className={styles.results}>
          <h3 className={styles.resultsTitle}>
            {lastResult.total_considered} result
            {lastResult.total_considered === 1 ? "" : "s"}
          </h3>
          {lastResult.items.length === 0 ? (
            <p className={styles.empty}>No matching cases.</p>
          ) : (
            <ul className={styles.hits}>
              {lastResult.items.map((h) => (
                <li key={h.id} className={styles.hit}>
                  <a href={`/cases/${h.id}`}>
                    <strong>{h.reference}</strong> — {h.title}
                  </a>
                  <span className={styles.meta}>
                    {h.priority} · {h.status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </section>
  );
}

function summarizeShape(
  query: string | null,
  filters: SearchFilterRequest[],
  sort: { field: string; direction: string }[],
): string {
  const parts: string[] = [];
  if (query) parts.push(`query: ${query}`);
  for (const f of filters) {
    parts.push(`${f.field} ${f.op} ${formatValue(f.value)}`);
  }
  if (sort.length > 0) {
    parts.push(
      `sort: ${sort.map((s) => `${s.field} ${s.direction}`).join(", ")}`,
    );
  }
  return parts.length === 0 ? "(no filters)" : parts.join(" · ");
}

function formatValue(value: string | number | boolean | string[]): string {
  if (Array.isArray(value)) return `[${value.join(", ")}]`;
  return String(value);
}