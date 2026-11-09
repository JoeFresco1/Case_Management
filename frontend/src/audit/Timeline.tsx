import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiClientError } from "../api/client";
import styles from "./Timeline.module.css";

/**
 * Per-case audit timeline (P09-US1 / US4; FM-043; FR-022).
 *
 * The component renders a chronological list of audit events for the
 * case identified by ``caseId``. Each row carries actor, event type,
 * timestamp, payload (redacted server-side), and the case-type /
 * workflow version that was in effect when the event was recorded.
 *
 * Filters:
 * - **Event type** (multi-select)
 * - **Actor** (UUID)
 * - **Since** / **Until** (ISO-8601 date-time)
 *
 * Pagination is cursor-based; the server returns ``next_cursor`` and
 * the client follows it on "Load more" clicks. The cursor is opaque
 * on the wire.
 *
 * The wire shapes are imported from the generated OpenAPI contract so this
 * view stays aligned with the API as the timeline evolves.
 */

interface TimelineFilters {
  eventTypes: string[];
  actorId: string;
  since: string;
  until: string;
}

const ALL_EVENT_TYPES = [
  "CaseCreated",
  "CaseUpdated",
  "WorkflowStarted",
  "WorkflowTransitioned",
  "DocumentAdded",
  "DecisionRecorded",
  "TaskCreated",
  "TaskCompleted",
  "AuditCorrectionRecorded",
] as const;

const DEFAULT_FILTERS: TimelineFilters = {
  eventTypes: [],
  actorId: "",
  since: "",
  until: "",
};

function formatTimestamp(iso: string): string {
  // The wire format is ISO-8601 with timezone; the browser's
  // `Date.toLocaleString()` renders it in the user's locale. A future
  // slice can swap in Intl.DateTimeFormat with explicit options.
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export interface TimelineProps {
  caseId: string;
}

export function Timeline({ caseId }: TimelineProps) {
  const [filters, setFilters] = useState<TimelineFilters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<TimelineFilters>(DEFAULT_FILTERS);
  const timeline = useInfiniteQuery({
    queryKey: ["timeline", caseId, applied],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.getCaseTimeline(caseId, {
        limit: 50,
        event_type: applied.eventTypes,
        actor_id: applied.actorId || undefined,
        since: applied.since
          ? new Date(applied.since).toISOString()
          : undefined,
        until: applied.until
          ? new Date(applied.until).toISOString()
          : undefined,
        cursor: pageParam,
      }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    staleTime: 15_000,
  });
  const pages = timeline.data?.pages.flatMap((page) => page.items) ?? [];
  function toggleEventType(t: string) {
    setFilters((f) => ({
      ...f,
      eventTypes: f.eventTypes.includes(t)
        ? f.eventTypes.filter((x) => x !== t)
        : [...f.eventTypes, t],
    }));
  }

  function applyFilters() {
    setApplied(filters);
  }

  function clearFilters() {
    setFilters(DEFAULT_FILTERS);
    setApplied(DEFAULT_FILTERS);
  }

  if (timeline.isLoading) return <p>Loading timeline…</p>;
  if (timeline.error && !timeline.data) {
    return (
      <p>
        {timeline.error instanceof ApiClientError
          ? timeline.error.message
          : "Failed to load timeline."}
      </p>
    );
  }

  return (
    <section data-testid="timeline" className={styles.stream}>
      <h2>Case activity</h2>
      <details className={styles.filters}>
        <summary>Filter activity</summary>
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
          <fieldset>
            <legend>Event types</legend>
            {ALL_EVENT_TYPES.map((t) => (
              <label key={t}>
                <input
                  type="checkbox"
                  checked={filters.eventTypes.includes(t)}
                  onChange={() => toggleEventType(t)}
                />
                {t.replace(/([a-z])([A-Z])/g, "$1 $2")}
              </label>
            ))}
          </fieldset>
          <button type="button" onClick={applyFilters}>
            Apply
          </button>
          <button type="button" onClick={clearFilters}>
            Clear
          </button>
        </fieldset>
      </details>
      {pages.length === 0 ? (
        <p>No events match the current filters.</p>
      ) : (
        <ol data-testid="timeline-list">
          {pages.map((entry) => (
            <li key={entry.id} data-testid="timeline-entry">
              <strong>
                {entry.event_type.replace(/([a-z])([A-Z])/g, "$1 $2")}
              </strong>{" "}
              · <span>{formatTimestamp(entry.occurred_at)}</span>
              <details>
                <summary>Event details</summary>
                <div>
                  Event type: <code>{entry.event_type}</code>
                  <br />
                  Actor: <code>{entry.actor_id}</code> ({entry.actor_type})
                </div>
                {entry.config_version ? (
                  <div>
                    Config version: <code>{entry.config_version}</code>
                  </div>
                ) : null}
                {entry.redacted ? (
                  <div>
                    <em>Some payload fields are redacted.</em>
                  </div>
                ) : null}
                <details>
                  <summary>Event data</summary>
                  <pre>{JSON.stringify(entry.payload, null, 2)}</pre>
                </details>
              </details>
            </li>
          ))}
        </ol>
      )}
      {timeline.isFetchNextPageError && (
        <p role="alert">Unable to load more activity. Try again.</p>
      )}
      {timeline.hasNextPage ? (
        <button
          type="button"
          disabled={timeline.isFetchingNextPage}
          onClick={() => void timeline.fetchNextPage()}
        >
          Load more
        </button>
      ) : null}
    </section>
  );
}

export default Timeline;
