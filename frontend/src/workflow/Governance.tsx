import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError } from "../api/client";

/**
 * Configuration governance timeline (FM-021 / P10-US4, US5, US8).
 *
 * The component renders the publication history for a workflow or
 * case-type version. Each row carries the lifecycle action (draft /
 * validate / publish / retire), the actor who performed it, the
 * occurred-at timestamp, and the human-readable change summary the
 * administrator entered at publish / retire time.
 *
 * Filters:
 * - **Artifact type** (``CaseTypeVersion`` / ``WorkflowVersion``)
 * - **Artifact id** (UUID; when set, scopes the result to a single
 *   artifact)
 * - **Limit / offset** (paginated; "Load more" follows the next
 *   offset)
 *
 * The component intentionally does NOT hand-type the OpenAPI
 * generated types — it consumes the same generated module the rest
 * of the frontend uses (FR-004 / AGENTS.md §10). For this slice the
 * wire shape is hand-built; once ``openapi-typescript`` is re-run the
 * local ``PublicationRecord`` mirror is removed.
 */
export interface PublicationRecord {
  id: string;
  tenant_id: string;
  artifact_type: string;
  artifact_id: string | null;
  version_id: string | null;
  action: "draft" | "validate" | "publish" | "retire";
  actor_id: string;
  occurred_at: string;
  change_summary: string;
}

interface GovernanceFilters {
  artifactType: string;
  artifactId: string;
  limit: number;
  offset: number;
}

const DEFAULT_FILTERS: GovernanceFilters = {
  artifactType: "",
  artifactId: "",
  limit: 50,
  offset: 0,
};

function buildSearch(filters: GovernanceFilters): string {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit));
  params.set("offset", String(filters.offset));
  if (filters.artifactType.trim()) {
    params.set("artifact_type", filters.artifactType.trim());
  }
  if (filters.artifactId.trim()) {
    params.set("artifact_id", filters.artifactId.trim());
  }
  return params.toString();
}

async function fetchPublications(
  filters: GovernanceFilters,
): Promise<{ items: PublicationRecord[] }> {
  const qs = buildSearch(filters);
  const response = await fetch(`/api/v1/admin/publications?${qs}`, {
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
      body.message ?? `publications request failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as { items: PublicationRecord[] };
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export interface GovernanceProps {
  /**
   * Optional default artifact id (the workflow / case-type the admin
   * opened this view from). When set, the component starts scoped to
   * that artifact; the admin can clear the filter to view the
   * tenant-wide timeline.
   */
  defaultArtifactId?: string;
  defaultArtifactType?: string;
}

export function Governance({
  defaultArtifactId = "",
  defaultArtifactType = "",
}: GovernanceProps) {
  const [filters, setFilters] = useState<GovernanceFilters>({
    ...DEFAULT_FILTERS,
    artifactType: defaultArtifactType,
    artifactId: defaultArtifactId,
  });
  const [applied, setApplied] = useState<GovernanceFilters>(filters);
  const [pages, setPages] = useState<PublicationRecord[]>([]);
  const [hasMore, setHasMore] = useState(false);

  const initialQuery = useQuery({
    queryKey: ["governance", "publications", applied],
    queryFn: () => fetchPublications(applied),
    staleTime: 15_000,
  });

  if (initialQuery.data && pages !== initialQuery.data.items) {
    setPages(initialQuery.data.items);
    setHasMore(initialQuery.data.items.length === applied.limit);
  }

  function applyFilters() {
    const reset = { ...filters, offset: 0 };
    setApplied(reset);
    setPages([]);
  }

  function clearFilters() {
    const cleared = { ...DEFAULT_FILTERS };
    setFilters(cleared);
    setApplied(cleared);
    setPages([]);
  }

  async function loadMore() {
    const nextFilters = { ...applied, offset: applied.offset + applied.limit };
    const next = await fetchPublications(nextFilters);
    setPages((prev) => [...prev, ...next.items]);
    setApplied(nextFilters);
    setHasMore(next.items.length === nextFilters.limit);
  }

  if (initialQuery.isLoading) return <p>Loading governance history…</p>;
  if (initialQuery.error) {
    return (
      <p data-testid="governance-error">
        {initialQuery.error instanceof ApiClientError
          ? initialQuery.error.message
          : "Failed to load governance history."}
      </p>
    );
  }

  return (
    <section data-testid="governance">
      <h2>Configuration governance</h2>
      <fieldset>
        <legend>Filters</legend>
        <label>
          Artifact type
          <input
            type="text"
            value={filters.artifactType}
            placeholder="CaseTypeVersion or WorkflowVersion"
            onChange={(e) =>
              setFilters((f) => ({ ...f, artifactType: e.target.value }))
            }
          />
        </label>
        <label>
          Artifact id
          <input
            type="text"
            value={filters.artifactId}
            placeholder="00000000-0000-0000-0000-000000000000"
            onChange={(e) =>
              setFilters((f) => ({ ...f, artifactId: e.target.value }))
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
        <p>No publications match the current filters.</p>
      ) : (
        <ol data-testid="governance-list">
          {pages.map((p) => (
            <li key={p.id} data-testid="governance-entry">
              <strong>{p.action}</strong>
              <span> · {p.artifact_type}</span>
              <span> · {formatTimestamp(p.occurred_at)}</span>
              <div>
                Actor: <code>{p.actor_id}</code>
              </div>
              {p.change_summary ? (
                <div>
                  Summary: <em>{p.change_summary}</em>
                </div>
              ) : null}
              {p.version_id ? (
                <div>
                  Version: <code>{p.version_id}</code>
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      )}
      {hasMore ? (
        <button type="button" onClick={loadMore}>
          Load more
        </button>
      ) : null}
    </section>
  );
}

export default Governance;
