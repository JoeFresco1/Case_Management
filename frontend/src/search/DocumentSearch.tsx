import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";

type DocumentSearchResponse = components["schemas"]["DocumentSearchResponse"];

import styles from "./DocumentSearch.module.css";

/**
 * In-case document search component (P03-US3 / P06-US8 / FM-033).
 *
 * The component is intentionally minimal: a free-text input (matched
 * against ``file_name`` via portable LIKE), an optional
 * classification dropdown, optional uploader + date-range filters,
 * and a result list. Re-issuance is via a TanStack ``useMutation``
 * so the request fires only on submit (avoids hammering the
 * backend as the user types in the free-text field).
 *
 * The wire shape is a generic ``DocumentSearchHit`` so the snippet
 * can carry either a bounded string excerpt or a ``Redacted``
 * marker for documents the actor cannot see (P11-US6 / FM-029).
 */
export interface DocumentSearchProps {
  /** The case id whose documents the actor wants to search. */
  caseId: string;
  /**
   * Optional pre-declared classifications on the case type. When
   * provided, the classification dropdown is bounded to this list so
   * the worker cannot submit a classification the case type does
   * not admit (the API rejects it with 400 otherwise).
   */
  allowedClassifications?: readonly string[];
}

export function DocumentSearch({
  caseId,
  allowedClassifications,
}: DocumentSearchProps) {
  const [query, setQuery] = useState("");
  const [classification, setClassification] = useState("");
  const [uploadedBy, setUploadedBy] = useState("");
  const [uploadedSince, setUploadedSince] = useState("");
  const [uploadedUntil, setUploadedUntil] = useState("");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);

  const search = useMutation({
    mutationFn: (nextOffset: number) =>
      api.searchCaseDocuments(caseId, {
        query: query || undefined,
        classification: classification || undefined,
        uploaded_by: uploadedBy || undefined,
        uploaded_since: uploadedSince || undefined,
        uploaded_until: uploadedUntil || undefined,
        limit,
        offset: nextOffset,
      }),
  });

  const result: DocumentSearchResponse | undefined = search.data;
  const error = search.error;

  return (
    <div className={styles.page}>
      <h2>Search documents in this case</h2>
      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          search.mutate(0);
        }}
      >
        <label className={styles.label}>
          Free-text query
          <input
            className={styles.input}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="matches file_name (case-insensitive substring)"
          />
        </label>
        <label className={styles.label}>
          Classification
          {allowedClassifications && allowedClassifications.length > 0 ? (
            <select
              className={styles.input}
              value={classification}
              onChange={(e) => setClassification(e.target.value)}
            >
              <option value="">(any)</option>
              {allowedClassifications.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : (
            <input
              className={styles.input}
              value={classification}
              onChange={(e) => setClassification(e.target.value)}
              placeholder="e.g. Contract"
            />
          )}
        </label>
        <label className={styles.label}>
          Uploader id
          <input
            className={styles.input}
            value={uploadedBy}
            onChange={(e) => setUploadedBy(e.target.value)}
            placeholder="UUID (optional)"
          />
        </label>
        <label className={styles.label}>
          Uploaded since
          <input
            className={styles.input}
            type="datetime-local"
            value={uploadedSince}
            onChange={(e) => setUploadedSince(e.target.value)}
          />
        </label>
        <label className={styles.label}>
          Uploaded until
          <input
            className={styles.input}
            type="datetime-local"
            value={uploadedUntil}
            onChange={(e) => setUploadedUntil(e.target.value)}
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
        <button className={styles.submit} type="submit" disabled={search.isPending}>
          {search.isPending ? "Searching…" : "Search"}
        </button>
      </form>

      {error instanceof ApiClientError ? (
        <p className={styles.error}>{error.message}</p>
      ) : null}

      {result ? (
        <section>
          <p className={styles.summary}>
            {result.total_considered} result
            {result.total_considered === 1 ? "" : "s"}
          </p>
          {result.items.length === 0 ? (
            <p className={styles.empty}>No matching documents.</p>
          ) : (
            <ul className={styles.list}>
              {result.items.map((h) => (
                <li key={h.id} className={styles.item}>
                  <div>
                    <strong>{h.file_name}</strong>{" "}
                    <span className={styles.meta}>
                      · {h.classification} · {h.sensitivity} · v
                      {h.current_version}
                    </span>
                  </div>
                  {typeof h.snippet === "string" && h.snippet.length > 0 ? (
                    <div className={styles.snippet}>
                      <code>{h.snippet}</code>
                    </div>
                  ) : h.snippet &&
                    typeof h.snippet === "object" &&
                    (h.snippet as { redacted?: unknown }).redacted === true ? (
                    <div className={styles.redacted}>
                      (restricted — match not visible)
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          <div className={styles.pager}>
            <button
              type="button"
              disabled={offset === 0 || search.isPending}
              onClick={() => {
                const next = Math.max(0, offset - limit);
                setOffset(next);
                search.mutate(next);
              }}
            >
              ← Previous
            </button>
            <button
              type="button"
              disabled={!result.next_cursor || search.isPending}
              onClick={() => {
                const next = offset + limit;
                setOffset(next);
                search.mutate(next);
              }}
            >
              Next →
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
