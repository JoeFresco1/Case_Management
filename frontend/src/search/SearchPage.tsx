import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { CaseSearchResponse, SortEntry } from "../api/types";
import { SavedSearches } from "./SavedSearches";

import { Icon } from "../shell/Icon";
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
  { value: "created_at", label: "Creation date" },
  { value: "reference", label: "Reference" },
  { value: "priority", label: "Priority" },
  { value: "status", label: "Status" },
  { value: "title", label: "Title" },
];

const PRIORITY_VALUES = ["low", "normal", "high", "urgent"];
const STATUS_VALUES = ["open", "in_progress", "on_hold", "closed", "cancelled"];

let filterIdCounter = 0;

function createFilterRow(
  field: string = "",
  isCore: boolean = false,
): FilterRow {
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
  const [sort, setSort] = useState<SortEntry>({
    field: "created_at",
    direction: "desc",
  });
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [showSavedSearches, setShowSavedSearches] = useState(false);

  const recent = useQuery({
    queryKey: ["cases", "list", { limit: 5, offset: 0 }],
    queryFn: () => api.listCases({ limit: 5, offset: 0 }),
    staleTime: 30_000,
  });
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      api.saveSearch({
        name: name.trim(),
        query: query || undefined,
        filters: buildFiltersForApi(),
        sort: [sort],
      }),
    onSuccess: () => {
      setSaving(false);
      setName("");
      void client.invalidateQueries({ queryKey: ["saved-searches"] });
    },
  });
  const search = useMutation({
    mutationFn: (nextOffset: number) =>
      api.searchCases({
        query: query || undefined,
        filters: filters
          .filter((f) => f.field && f.value)
          .map((f) => ({
            field: f.field,
            op: f.op,
            value:
              f.op === "in" ? f.value.split(",").map((v) => v.trim()) : f.value,
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
      prev.map((f) => (f.id === id ? { ...f, ...updates } : f)),
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
        value:
          f.op === "in" ? f.value.split(",").map((v) => v.trim()) : f.value,
      }));
  };

  return (
    <div className={styles.page}>
      <header className={styles.hero}>
        <div className={styles.heroText}>
          <h1>Case search</h1>
          <p>Find a case by title, or narrow your search by case details.</p>
        </div>
        <svg
          className={styles.art}
          viewBox="0 0 300 170"
          fill="none"
          aria-hidden="true"
        >
          <defs>
            <pattern
              id="search-grid"
              width="18"
              height="18"
              patternUnits="userSpaceOnUse"
            >
              <circle cx="2" cy="2" r="1" fill="#99bbf5" opacity=".42" />
            </pattern>
          </defs>
          <rect width="300" height="170" fill="url(#search-grid)" />
          <g
            transform="translate(25 8) rotate(-12 120 85)"
            strokeWidth="1.8"
            strokeLinejoin="round"
          >
            <path
              d="M63 42h38l10 10h79v76H63z"
              fill="#345da5"
              stroke="#83a7e3"
            />
            <path
              d="M51 52h38l11 11h79v76H51z"
              fill="#517abd"
              stroke="#adc4ea"
            />
            <path
              d="M42 68h39l10 10h77v68H42z"
              fill="#9dbdeb"
              stroke="#d7e7ff"
            />
            <path d="M54 93h56m-56 10h38" stroke="#466ba3" />
          </g>
          <circle
            cx="204"
            cy="106"
            r="24"
            fill="#223e73"
            stroke="#dceaff"
            strokeWidth="4"
          />
          <path
            d="m221 123 22 22"
            stroke="#dceaff"
            strokeWidth="8"
            strokeLinecap="round"
          />
        </svg>
      </header>
      <form className={styles.form} onSubmit={handleSubmit}>
        <div className={styles.searchRow}>
          <label className={styles.query}>
            <Icon name="search" />
            <input
              aria-label="Case title"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search case titles…"
            />
          </label>
          <button
            className={styles.submit}
            type="submit"
            disabled={search.isPending}
          >
            {search.isPending ? "Searching…" : "Search"}
          </button>
        </div>
        <div className={styles.toolbar}>
          <button
            type="button"
            className={styles.secondaryButton}
            aria-expanded={showSavedSearches}
            onClick={() => setShowSavedSearches(!showSavedSearches)}
          >
            Saved searches
          </button>
          <button
            type="button"
            className={styles.secondaryButton}
            aria-expanded={saving}
            onClick={() => {
              setSaving(!saving);
              save.reset();
            }}
          >
            Save this search
          </button>
        </div>
        <details className={styles.advanced}>
          <summary>
            <Icon name="settings" />
            Filters &amp; sorting
          </summary>
          <fieldset className={styles.fieldset}>
            <legend>Field filters</legend>
            <div className={styles.filterList}>
              {filters.map((filter) => (
                <div key={filter.id} className={styles.filterRow}>
                  <select
                    className={styles.filterSelect}
                    aria-label="Filter field"
                    value={filter.field}
                    onChange={(e) =>
                      updateFilter(filter.id, { field: e.target.value })
                    }
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
                    <option value="data.due_date">data.due_date</option>
                  </select>

                  <select
                    className={styles.filterSelect}
                    aria-label="Filter operator"
                    value={filter.op}
                    onChange={(e) =>
                      updateFilter(filter.id, {
                        op: e.target.value as FilterOperator,
                      })
                    }
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
                      aria-label={`Value for ${filter.field || "filter"}`}
                      value={filter.value}
                      onChange={(e) =>
                        updateFilter(filter.id, { value: e.target.value })
                      }
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
                      aria-label={`Value for ${filter.field || "filter"}`}
                      value={filter.value}
                      onChange={(e) =>
                        updateFilter(filter.id, { value: e.target.value })
                      }
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
                      aria-label={`Value for ${filter.field || "filter"}`}
                      type="text"
                      value={filter.value}
                      onChange={(e) =>
                        updateFilter(filter.id, { value: e.target.value })
                      }
                      placeholder="comma-separated values (e.g. low,normal,high)"
                    />
                  ) : (
                    <input
                      className={styles.filterInput}
                      aria-label={`Value for ${filter.field || "filter"}`}
                      type="text"
                      value={filter.value}
                      onChange={(e) =>
                        updateFilter(filter.id, { value: e.target.value })
                      }
                      placeholder={
                        filter.op === "like" ? "substring match" : "exact match"
                      }
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
              <button
                type="button"
                className={styles.addFilterButton}
                onClick={addFilter}
              >
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
                  onChange={(e) =>
                    setSort((s) => ({ ...s, field: e.target.value }))
                  }
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
                  onChange={(e) =>
                    setSort((s) => ({
                      ...s,
                      direction: e.target.value as "asc" | "desc",
                    }))
                  }
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
        </details>
      </form>
      {saving && (
        <form
          className={styles.saveForm}
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) save.mutate();
          }}
        >
          <label>
            Search name{" "}
            <input
              autoFocus
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <button
            className={styles.submit}
            disabled={!name.trim() || save.isPending}
          >
            Save
          </button>
        </form>
      )}
      {save.isSuccess && <p role="status">Search saved.</p>}
      {save.isError && <p role="alert">Unable to save this search.</p>}
      {showSavedSearches && <SavedSearches />}
      {!result && !search.isError && !search.isPending && (
        <section className={styles.recent}>
          <header>
            <h2>Recent cases</h2>
            <a href="/cases">View all cases →</a>
          </header>
          {recent.isLoading ? (
            <p>Loading cases…</p>
          ) : recent.isError ? (
            <p>Recent cases could not be loaded.</p>
          ) : recent.data?.items.length ? (
            recent.data.items.map((c) => (
              <a key={c.id} className={styles.caseRow} href={`/cases/${c.id}`}>
                <span className={styles.folder}>
                  <Icon name="cases" />
                </span>
                <span className={styles.caseName}>
                  <strong>{c.title}</strong>
                  <small>{c.reference}</small>
                </span>
                <span className={styles.status}>{c.status}</span>
                <span aria-hidden="true">›</span>
              </a>
            ))
          ) : (
            <p>No cases available.</p>
          )}
        </section>
      )}
      {error ? (
        <p className={styles.error}>
          {error instanceof ApiClientError
            ? error.message
            : "Unable to search cases."}
        </p>
      ) : null}

      {result ? (
        <section className={styles.results}>
          <p className={styles.summary}>
            {result.total_considered} result
            {result.total_considered === 1 ? "" : "s"}
          </p>
          {result.items.length === 0 ? (
            <p className={styles.empty}>No matching cases.</p>
          ) : (
            <ul className={styles.list}>
              {result.items.map((h) => (
                <li key={h.id} className={styles.item}>
                  <a className={styles.caseRow} href={`/cases/${h.id}`}>
                    <span className={styles.folder}>
                      <Icon name="cases" />
                    </span>
                    <span className={styles.caseName}>
                      <strong>{h.title}</strong>
                      <small>
                        {h.reference} · {h.priority} priority
                      </small>
                    </span>
                    <span className={styles.status}>{h.status}</span>
                    <span aria-hidden="true">›</span>
                  </a>
                  {Object.keys(h.data_snippet).some(
                    (key) => key !== "title" && key !== "priority",
                  ) ? (
                    <ul className={styles.snippet}>
                      {Object.entries(h.data_snippet)
                        .filter(
                          ([key]) => key !== "title" && key !== "priority",
                        )
                        .map(([k, v]) => (
                          <li key={k}>
                            <code>{k}</code>:{" "}
                            {isRedacted(v) ? (
                              <span className={styles.redacted}>
                                (restricted)
                              </span>
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
