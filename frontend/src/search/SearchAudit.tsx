import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { SearchAuditList } from "../api/types";

import styles from "./SearchAudit.module.css";

/**
 * Search audit page for security staff (P06-US5 / FM-032).
 *
 * Displays search activity including who searched for what, when,
 * and with which result count. Supports filtering by actor,
 * saved search, and date range.
 */

export function SearchAuditPage() {
  const [actorId, setActorId] = useState("");
  const [savedSearchId, setSavedSearchId] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);

  const query = useQuery({
    queryKey: ["search-audit", { actorId, savedSearchId, since, until, limit, offset }],
    queryFn: () =>
      api.listSearchAudit({
        actor_id: actorId || undefined,
        saved_search_id: savedSearchId || undefined,
        since: since || undefined,
        until: until || undefined,
        limit,
        offset,
      }),
    staleTime: 30_000,
  });

  const data: SearchAuditList | undefined = query.data;
  const error = query.error;

  const handlePrev = () => {
    setOffset((prev) => Math.max(0, prev - limit));
  };

  const handleNext = () => {
    if (data?.items.length === limit) {
      setOffset((prev) => prev + limit);
    }
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Search audit</h1>
        <p className={styles.description}>
          Security staff view: who searched for what, when, and with which result count.
        </p>
      </header>

      <form className={styles.filterForm} onSubmit={(e) => e.preventDefault()}>
        <fieldset className={styles.fieldset}>
          <legend>Filters</legend>
          <div className={styles.filterRow}>
            <label className={styles.label}>
              Actor ID (UUID)
              <input
                className={styles.input}
                type="text"
                value={actorId}
                onChange={(e) => setActorId(e.target.value)}
                placeholder="Filter by actor UUID"
              />
            </label>
            <label className={styles.label}>
              Saved search ID (UUID)
              <input
                className={styles.input}
                type="text"
                value={savedSearchId}
                onChange={(e) => setSavedSearchId(e.target.value)}
                placeholder="Filter by saved search UUID"
              />
            </label>
            <label className={styles.label}>
              Since (ISO-8601)
              <input
                className={styles.input}
                type="datetime-local"
                value={since}
                onChange={(e) => setSince(e.target.value)}
              />
            </label>
            <label className={styles.label}>
              Until (ISO-8601)
              <input
                className={styles.input}
                type="datetime-local"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
            </label>
            <label className={styles.label}>
              Limit
              <input
                className={styles.input}
                type="number"
                min={1}
                max={200}
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
              />
            </label>
          </div>
        </fieldset>
      </form>

      {error instanceof ApiClientError ? (
        <p className={styles.error}>{error.message}</p>
      ) : null}

      {query.isLoading ? (
        <p className={styles.loading}>Loading audit entries…</p>
      ) : data ? (
        <section className={styles.results}>
          <p className={styles.summary}>
            {data.items.length} audit entr{data.items.length === 1 ? "y" : "ies"} on this page
          </p>
          {data.items.length === 0 ? (
            <p className={styles.empty}>No audit entries match the current filters.</p>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Occurred at</th>
                  <th>Actor ID</th>
                  <th>Saved search</th>
                  <th>Query shape</th>
                  <th>Result count</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.id}>
                    <td className={styles.timestamp}>{formatTimestamp(item.occurred_at)}</td>
                    <td className={styles.actorId}>{item.actor_id}</td>
                    <td className={styles.savedSearchId}>
                      {item.saved_search_id ? (
                        <code>{item.saved_search_id}</code>
                      ) : (
                        <span className={styles.muted}>—</span>
                      )}
                    </td>
                    <td className={styles.queryShape}>
                      <pre>{JSON.stringify(item.query_shape, null, 2)}</pre>
                    </td>
                    <td className={styles.resultCount}>{item.result_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className={styles.pager}>
            <button
              type="button"
              disabled={offset === 0 || query.isFetching}
              onClick={handlePrev}
            >
              ← Previous
            </button>
            <button
              type="button"
              disabled={data.items.length < limit || query.isFetching}
              onClick={handleNext}
            >
              Next →
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function formatTimestamp(iso: string): string {
  try {
    const date = new Date(iso);
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}