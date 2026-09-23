import { useQuery } from "@tanstack/react-query";

import { api, isForbiddenLike } from "../api/client";
import { documentQueryKeys } from "./queryKeys";
import styles from "./Documents.module.css";

/**
 * Unified revision + rendition history for one document (T5 viewer).
 *
 * Versions are the immutable filed originals; renditions are burned
 * viewer bytes (e.g. redacted burns) derived from a source version.
 * The viewer renders them as one timeline so workers see
 * "v2 filed, redacted burn of v2, v3 filed" without joining endpoints.
 *
 * Forbidden/unknown documents render a metadata-free message: no
 * version numbers, hashes, or timestamps leak to an unauthorized actor.
 */
export function RevisionHistory({ documentId }: { documentId: string }) {
  const historyQuery = useQuery({
    queryKey: documentQueryKeys.revisions(documentId),
    queryFn: () => api.viewer.getRevisions(documentId),
    staleTime: 15_000,
  });

  if (historyQuery.isLoading) return <p>Loading revision history…</p>;
  if (historyQuery.error) {
    if (isForbiddenLike(historyQuery.error)) {
      return (
        <p className={styles.empty} role="alert">
          Revision history is restricted or not available.
        </p>
      );
    }
    return (
      <p className={styles.error} role="alert">
        Revision history could not be loaded.
      </p>
    );
  }

  const entries = historyQuery.data?.entries ?? [];
  if (entries.length === 0) {
    return <p className={styles.empty}>No revisions recorded.</p>;
  }

  return (
    <section aria-label="Revision history">
      <h3>Revision history</h3>
      <ol className={styles.list}>
        {entries.map((entry, index) => (
          <li
            key={`${entry.entry_type}-${entry.version_number}-${entry.occurred_at}-${index}`}
            className={styles.item}
          >
            <span className={styles.badge}>{entry.entry_type}</span>
            <span className={styles.badge}>{entry.kind}</span>
            <strong>v{entry.version_number}</strong>
            <div className={styles.meta}>
              <code>{entry.sha256.slice(0, 12)}…</code> · {entry.size} bytes ·{" "}
              <time dateTime={entry.occurred_at}>
                {new Date(entry.occurred_at).toLocaleString()}
              </time>
            </div>
            {entry.rendition_id && (
              <div className={styles.meta}>rendition {entry.rendition_id}</div>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
