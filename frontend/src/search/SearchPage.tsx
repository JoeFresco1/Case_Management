import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { CaseSearchResponse, SortEntry } from "../api/types";
import { SavedSearches } from "./SavedSearches";

import styles from "./SearchPage.module.css";

/**
 * Authorized case search page (P06 / FM-031).
 *
 * Features:
 * - Free-text query (matches case title)
 * - Core field filters (priority, status) with eq/like/in operators
 * - Dynamic data.* field filters with operator selection
 * - Sort options
 * - Pagination
 * - Save search integration
 * - Redaction-aware snippet rendering
 */

type FilterOperator = "eq" | "like" | "in";
type CoreField = "priority" | "status" | "reference" | "current_workflow_state";

interface FilterRow {
  id: number;
  field: string;
  op: FilterOperator;
  value: string;
  isCore: boolean;
}

const CORE_FIELDS: { value: CoreField; label: string }[] = [
  { value: "priority", label: "Priority" },
  { value: "status", label: "Status" },
  { value: "reference", label: "Reference" },
  { value: "current_workflow_state", label: "Workflow State" },
];

const OPERATORS: { value: FilterOperator; label: string }[] = [
  { value: "eq", label: "Equals" },
  { value: "like", label: "Contains" },
  { value: "in", label: "In list (comma-separated)" },
];

const SORT_FIELDS: { value: string; label: string }[] = [
  { value: "created_at", label: "Created (newest first)" },
  { value: "-created_at", label: "Created (oldest first)" },
  { value: "reference", label: "Reference" },
  { value: "priority", label: "Priority" },
  { value: "status", label: "Status" },
  { value: "title", label: "Title" },
];

const PRIORITY_VALUES = ["low", "normal", "high", "urgent"];
const STATUS_VALUES = ["open", "in_progress", "on_hold", "closed", "cancelled"];

let filterIdCounter = 0;

function createFilterRow(field: string = "", isCore: boolean = false): FilterRow {
  return {
    id: ++filterIdCounter,
    field,
    op: "eq",
    value: "",
    isCore,
  };
}

export function SearchPage() {
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<FilterRow[]>([
    createFilterRow("priority", true),
    createFilterRow("status", true),
  ]);
  const [sort, setSort] = useState<SortEntry>({ field: "created_at", direction: "desc" });
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [showSavedSearches, setShowSavedSearches] = useState(false);

  const search = useMutation({
    mutationFn: (nextOffset: number) =>
      api.searchCases({
        query: query || undefined,
        filters: filters
          .filter((f) => f.field && f.value)
          .map((f) => ({
            field: f.field,
            op: f.op,
            value: f.op === "in" ? f.value.split(",").map((v) => v.trim()) : f.value,
          })),
        sort: [sort],
        limit,
        offset: nextOffset,
      }),
  });

  const result: CaseSearchResponse | undefined = search.data;
  const error = search.error;

  const addFilter = () => {
    setFilters((prev) => [...prev, createFilterRow()]);
  };

  const removeFilter = (id: number) => {
    if (filters.length <= 1) return;
    setFilters((prev) => prev.filter((f) => f.id !== id));
  };

  const updateFilter = (id: number, updates: Partial<FilterRow>) => {
    setFilters((prev) =>
      prev.map((f) => (f.id === id ? { ...f, ...updates } : f))
    );
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setOffset(0);
    search.mutate(0);
  };

  const buildFiltersForApi = () => {
    return filters
      .filter((f) => f.field && f.value)
      .map((f) => ({
        field: f.field,
        op: f.op,
        value: f.op === "in" ? f.value.split(",").map((v) => v.trim()) : f.value,
      }));
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>Search cases</h1>
        <div className={styles.headerActions}>
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => setShowSavedSearches((v) => !v)}
          >
            {showSavedSearches ? "Hide" : "Show"} saved searches
          </button>
          <a className={styles.secondaryButton} href="/search/audit">
            View search audit
          </a>
        </div>
      </header>
      <p className={styles.meta}>
        Every search (ad-hoc or saved) writes an audit row with the query shape
        and result count — no payload data. Security staff can inspect it under{" "}
        <a href="/search/audit">Search audit</a>.
      </p>

      <form className={styles.form} onSubmit={handleSubmit}>
        <fieldset className={styles.fieldset}>
          <legend>Free-text query</legend>
          <label className={styles.label}>
            <input
              className={styles.input}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="matches case title (substring, case-insensitive)"
            />
          </label>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend>Field filters</legend>
          <div className={styles.filterList}>
            {filters.map((filter) => (
              <div key={filter.id} className={styles.filterRow}>
                <select
                  className={styles.filterSelect}
                  value={filter.field}
                  onChange={(e) => updateFilter(filter.id, { field: e.target.value })}
                  disabled={filter.isCore}
                >
                  {CORE_FIELDS.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.label} (core)
                    </option>
                  ))}
                  <option value="data.custom" disabled>
                    ─── Custom data.* field ───
                  </option>
                  <option value="data.participantName">
                    data.participantName
                  </option>
                  <option value="data.due_date">
                    data.due_date
                  </option>
                </select>

                <select
                  className={styles.filterSelect}
                  value={filter.op}
                  onChange={(e) => updateFilter(filter.id, { op: e.target.value as FilterOperator })}
                >
                  {OPERATORS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>

                {filter.field === "priority" && filter.op === "eq" ? (
                  <select
                    className={styles.filterInput}
                    value={filter.value}
                    onChange={(e) => updateFilter(filter.id, { value: e.target.value })}
                  >
                    <option value="">(any)</option>
                    {PRIORITY_VALUES.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                ) : filter.field === "status" && filter.op === "eq" ? (
                  <select
                    className={styles.filterInput}
                    value={filter.value}
                    onChange={(e) => updateFilter(filter.id, { value: e.target.value })}
                  >
                    <option value="">(any)</option>
                    {STATUS_VALUES.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                ) : filter.op === "in" ? (
                  <input
                    className={styles.filterInput}
                    type="text"
                    value={filter.value}
                    onChange={(e) => updateFilter(filter.id, { value: e.target.value })}
                    placeholder="comma-separated values (e.g. low,normal,high)"
                  />
                ) : (
                  <input
                    className={styles.filterInput}
                    type="text"
                    value={filter.value}
                    onChange={(e) => updateFilter(filter.id, { value: e.target.value })}
                    placeholder={filter.op === "like" ? "substring match" : "exact match"}
                  />
                )}

                <button
                  type="button"
                  className={styles.removeButton}
                  onClick={() => removeFilter(filter.id)}
                  disabled={filters.length <= 1}
                  aria-label="Remove filter"
                >
                  ×
                </button>
              </div>
            ))}
            <button type="button" className={styles.addFilterButton} onClick={addFilter}>
              + Add filter
            </button>
          </div>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend>Sort & Pagination</legend>
          <div className={styles.sortRow}>
            <label className={styles.label}>
              Sort by
              <select
                className={styles.input}
                value={sort.field}
                onChange={(e) => setSort((s) => ({ ...s, field: e.target.value }))}
              >
                {SORT_FIELDS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.label}>
              Order
              <select
                className={styles.input}
                value={sort.direction}
                onChange={(e) => setSort((s) => ({ ...s, direction: e.target.value as "asc" | "desc" }))}
              >
                <option value="desc">Descending</option>
                <option value="asc">Ascending</option>
              </select>
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

        <div className={styles.actions}>
          <button className={styles.submit} type="submit" disabled={search.isPending}>
            {search.isPending ? "Searching…" : "Search"}
          </button>
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => {
              const name = prompt("Enter a name for this saved search:");
              if (name) {
                api.saveSearch({
                  name,
                  query: query || undefined,
                  filters: buildFiltersForApi(),
                  sort: [sort],
                }).then(() => {
                  alert("Search saved successfully!");
                }).catch((err) => {
                  alert(`Failed to save search: ${err.message}`);
                });
              }
            }}
            disabled={search.isPending}
          >
            Save search
          </button>
        </div>
      </form>

      {error instanceof ApiClientError ? (
        <p className={styles.error}>{error.message}</p>
      ) : null}

      {result ? (
        <section className={styles.results}>
          <p className={styles.summary}>
            {result.total_considered} result{result.total_considered === 1 ? "" : "s"}
          </p>
          {result.items.length === 0 ? (
            <p className={styles.empty}>No matching cases.</p>
          ) : (
            <ul className={styles.list}>
              {result.items.map((h) => (
                <li key={h.id} className={styles.item}>
                  <a href={`/cases/${h.id}`}>
                    <strong>{h.reference}</strong> — {h.title}
                  </a>
                  <div className={styles.meta}>
                    <span>{h.priority}</span> · <span>{h.status}</span>
                  </div>
                  {Object.keys(h.data_snippet).length > 0 ? (
                    <ul className={styles.snippet}>
                      {Object.entries(h.data_snippet).map(([k, v]) => (
                        <li key={k}>
                          <code>{k}</code>:{" "}
                          {isRedacted(v) ? (
                            <span className={styles.redacted}>(restricted)</span>
                          ) : (
                            <span>{String(v)}</span>
                          )}
                        </li>
                      ))}
                    </ul>
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

      {showSavedSearches && <SavedSearches />}
    </div>
  );
}

function isRedacted(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { redacted?: unknown }).redacted === true
  );
}