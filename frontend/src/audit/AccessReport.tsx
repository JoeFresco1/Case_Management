import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError } from "../api/client";
import type { Page } from "../api/types";

/**
 * Security access-decision report (P09-US6 / FM-045; P11-US9).
 *
 * A security administrator uses this view to answer "who tried what,
 * against which resource, and why". Every authorization decision —
 * allowed and denied — is on the audit chain; this component fetches
 * the rows through ``GET /api/v1/audit/access-decisions`` and lets
 * the operator narrow by actor, action, resource kind, and result
 * (allowed / denied).
 *
 * The wire shapes are generated from OpenAPI; the local mirror
 * below is hand-built ahead of the next ``openapi-typescript`` run.
 */
interface AccessDecision {
  id: string;
  occurred_at: string;
  actor_id: string;
  actor_type: string;
  actor_roles: string[];
  action: string;
  resource_kind: string;
  resource_id: string | null;
  result: "allowed" | "denied";
  reason: string;
  correlation_id: string;
}

interface AccessReportFilters {
  actorId: string;
  action: string;
  resourceKind: string;
  result: "" | "allowed" | "denied";
  since: string;
  until: string;
}

const DEFAULT_FILTERS: AccessReportFilters = {
  actorId: "",
  action: "",
  resourceKind: "",
  result: "",
  since: "",
  until: "",
};

function buildSearch(
  filters: AccessReportFilters,
  cursor?: string,
): string {
  const params = new URLSearchParams();
  params.set("limit", "50");
  if (filters.actorId.trim()) params.set("actor_id", filters.actorId.trim());
  if (filters.action.trim()) params.set("action", filters.action.trim());
  if (filters.resourceKind.trim())
    params.set("subject_type", filters.resourceKind.trim());
  if (filters.result) params.set("result", filters.result);
  if (filters.since.trim()) params.set("since", filters.since.trim());
  if (filters.until.trim()) params.set("until", filters.until.trim());
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

async function fetchAccessPage(
  filters: AccessReportFilters,
  cursor: string | undefined,
): Promise<Page<AccessDecision>> {
  const qs = buildSearch(filters, cursor);
  const response = await fetch(`/api/v1/audit/access-decisions?${qs}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    let body: { code?: string; message?: string } = {};
    try {
      body = await response.json();
    } catch {
      /* ignore */
    }
    throw new ApiClientError(
      body.message ?? `access report failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as Page<AccessDecision>;
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function AccessReport() {
  const [filters, setFilters] = useState<AccessReportFilters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<AccessReportFilters>(DEFAULT_FILTERS);
  const [pages, setPages] = useState<AccessDecision[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const initialQuery = useQuery({
    queryKey: ["access-decisions", applied],
    queryFn: () => fetchAccessPage(applied, undefined),
    staleTime: 15_000,
  });

  if (initialQuery.data && pages !== initialQuery.data.items) {
    setPages(initialQuery.data.items);
    setNextCursor(initialQuery.data.next_cursor);
  }

  function applyFilters() {
    setApplied(filters);
    setPages([]);
    setNextCursor(null);
    initialQuery.refetch();
  }

  function clearFilters() {
    setFilters(DEFAULT_FILTERS);
    setApplied(DEFAULT_FILTERS);
    setPages([]);
    setNextCursor(null);
  }

  async function loadMore() {
    if (!nextCursor) return;
    const next = await fetchAccessPage(applied, nextCursor);
    setPages((prev) => [...prev, ...next.items]);
    setNextCursor(next.next_cursor);
  }

  if (initialQuery.isLoading) return <p>Loading access report…</p>;
  if (initialQuery.error) {
    return (
      <p>
        {initialQuery.error instanceof ApiClientError
          ? initialQuery.error.message
          : "Failed to load access report."}
      </p>
    );
  }

  return (
    <section data-testid="access-report">
      <h2>Access decisions</h2>
      <fieldset>
        <legend>Filters</legend>
        <label>
          Actor UUID
          <input
            type="text"
            value={filters.actorId}
            placeholder="00000000-0000-0000-0000-000000000000"
            onChange={(e) =>
              setFilters((f) => ({ ...f, actorId: e.target.value }))
            }
          />
        </label>
        <label>
          Action
          <input
            type="text"
            value={filters.action}
            placeholder="case.read_sensitive"
            onChange={(e) =>
              setFilters((f) => ({ ...f, action: e.target.value }))
            }
          />
        </label>
        <label>
          Resource kind
          <input
            type="text"
            value={filters.resourceKind}
            placeholder="case"
            onChange={(e) =>
              setFilters((f) => ({ ...f, resourceKind: e.target.value }))
            }
          />
        </label>
        <label>
          Result
          <select
            value={filters.result}
            onChange={(e) =>
              setFilters((f) => ({
                ...f,
                result: e.target.value as AccessReportFilters["result"],
              }))
            }
          >
            <option value="">Both</option>
            <option value="allowed">Allowed</option>
            <option value="denied">Denied</option>
          </select>
        </label>
        <label>
          Since
          <input
            type="datetime-local"
            value={filters.since}
            onChange={(e) =>
              setFilters((f) => ({ ...f, since: e.target.value }))
            }
          />
        </label>
        <label>
          Until
          <input
            type="datetime-local"
            value={filters.until}
            onChange={(e) =>
              setFilters((f) => ({ ...f, until: e.target.value }))
            }
          />
        </label>
        <button type="button" onClick={applyFilters}>
          Apply
        </button>
        <button type="button" onClick={clearFilters}>
          Clear
        </button>
      </fieldset>

      {pages.length === 0 ? (
        <p>No access decisions match the current filters.</p>
      ) : (
        <ol data-testid="access-decisions-list">
          {pages.map((d) => (
            <li key={d.id} data-testid="access-decision-entry">
              <strong data-testid="access-result">
                {d.result.toUpperCase()}
              </strong>{" "}
              · <span>{formatTimestamp(d.occurred_at)}</span>
              <div>
                Action: <code>{d.action}</code>
              </div>
              <div>
                Resource: <code>{d.resource_kind}</code>
                {d.resource_id ? <> · <code>{d.resource_id}</code></> : null}
              </div>
              <div>
                Actor: <code>{d.actor_id}</code> ({d.actor_type})
                {d.actor_roles.length > 0 ? (
                  <> · roles: {d.actor_roles.join(", ")}</>
                ) : null}
              </div>
              <div>
                Reason: <em>{d.reason}</em>
              </div>
              <div>
                Correlation: <code>{d.correlation_id}</code>
              </div>
            </li>
          ))}
        </ol>
      )}
      {nextCursor ? (
        <button type="button" onClick={loadMore}>
          Load more
        </button>
      ) : null}
    </section>
  );
}

export default AccessReport;