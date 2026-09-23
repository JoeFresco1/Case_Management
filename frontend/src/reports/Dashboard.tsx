import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Manager KPI dashboard (P12-US8 / FM-054).
 *
 * The component renders a card for each KPI sourced from
 * ``GET /api/v1/reports/dashboards/operational``:
 *
 * - total open cases on the actor's team
 * - total overdue tasks on the actor's team
 * - a per-priority breakdown of the open-cases count
 *
 * Each card carries the dashboard's explicit ``generated_at`` and
 * ``freshness_seconds`` fields so the manager can see exactly when
 * the system of record was sampled (P12 §1) and how stale the data
 * is (FM-054 promises sub-five-second freshness). The component is
 * read-only; reports do not mutate state.
 *
 * The response type is generated from the backend OpenAPI contract and
 * consumed through the shared API client, so identity and error handling
 * remain owned by the application shell rather than this report view.
 */

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString();
}

function formatFreshness(seconds: number): string {
  if (seconds <= 0) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ago`;
}

function KpiCard(props: {
  label: string;
  value: number;
  hint?: string;
  tone?: "neutral" | "warning";
}) {
  const toneClass =
    props.tone === "warning" ? styles.error : styles.muted;
  return (
    <article className={styles.header}>
      <h3>{props.label}</h3>
      <p className={styles.muted} aria-live="polite">
        <strong>{props.value.toLocaleString()}</strong>
        {props.hint ? (
          <>
            {" "}
            <span className={toneClass}>· {props.hint}</span>
          </>
        ) : null}
      </p>
    </article>
  );
}

function PriorityBreakdown(props: { buckets: Record<string, number> }) {
  const entries = Object.entries(props.buckets).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  if (entries.length === 0) {
    return (
      <p className={styles.empty}>No priority data on this team.</p>
    );
  }
  return (
    <ul className={styles.table}>
      {entries.map(([priority, count]) => (
        <li key={priority}>
          <strong>{priority}</strong>: {count.toLocaleString()}
        </li>
      ))}
    </ul>
  );
}

export interface DashboardProps {
  /** Optional override of the polling interval in ms. Defaults to 4s
   *  to stay inside the FM-054 sub-five-second freshness window. */
  refreshIntervalMs?: number;
}

export function Dashboard(props: DashboardProps) {
  const refreshIntervalMs = props.refreshIntervalMs ?? 4_000;
  const query = useQuery({
    queryKey: ["reports", "dashboard", "operational"],
    queryFn: api.getOperationalDashboard,
    refetchInterval: refreshIntervalMs,
    staleTime: 1_000,
  });

  return (
    <section className={styles.workspace} aria-label="Operational dashboard">
      <header className={styles.header}>
        <h2>Operational KPIs</h2>
        {query.data ? (
          <p className={styles.muted}>
            team:{" "}
            {query.data.team.length === 0
              ? "(none)"
              : query.data.team.join(", ")}{" "}
            · source time {formatTimestamp(query.data.generated_at)} ·{" "}
            {formatFreshness(query.data.freshness_seconds)}
          </p>
        ) : null}
      </header>

      {query.isLoading ? (
        <p>Loading dashboard…</p>
      ) : query.error ? (
        <p className={styles.error}>{query.error.message}</p>
      ) : query.data ? (
        <>
          <KpiCard
            label="Open cases"
            value={query.data.kpis.total_open_cases}
            hint={`generated_at ${formatTimestamp(query.data.generated_at)}`}
          />
          <KpiCard
            label="Overdue tasks"
            value={query.data.kpis.total_overdue_tasks}
            hint={`freshness ${query.data.freshness_seconds}s`}
            tone={
              query.data.kpis.total_overdue_tasks > 0 ? "warning" : "neutral"
            }
          />
          <section>
            <h3>Open cases by priority</h3>
            <PriorityBreakdown
              buckets={query.data.kpis.open_by_priority ?? {}}
            />
          </section>
        </>
      ) : null}
    </section>
  );
}

export default Dashboard;
