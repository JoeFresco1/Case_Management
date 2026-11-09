/** Live team workload, sampled from the operational reporting endpoint. */
import { useQuery } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import styles from "./Dashboard.module.css";

const priorities = ["urgent", "high", "normal", "low", "other"];

/** Show the API's priority counts and each bucket's share of the open caseload. */
function PriorityBreakdown({
  buckets,
  total,
}: {
  buckets: Record<string, number>;
  total: number;
}) {
  const order = (name: string) =>
    priorities.includes(name) ? priorities.indexOf(name) : priorities.length;
  const entries = Object.entries(buckets).sort(
    ([a], [b]) => order(a) - order(b) || a.localeCompare(b),
  );
  if (!total || !entries.length) {
    return (
      <p className={styles.empty}>
        {total === 0
          ? "Your team has no open cases."
          : "The reporting service returned no priority breakdown for this snapshot."}
      </p>
    );
  }
  return (
    <ul className={styles.chart} aria-label="Priority distribution">
      {entries.map(([priority, count]) => {
        const share = (count / total) * 100;
        return (
          <li key={priority}>
            <span className={styles.label}>{priority}</span>
            <div className={styles.track} aria-hidden="true">
              <span
                data-priority={priority}
                style={{ width: `${Math.min(100, share)}%` }}
              />
            </div>
            <strong aria-label={`${count} ${count === 1 ? "case" : "cases"}`}>
              {count.toLocaleString()}
            </strong>
            <small>{Math.round(share)}%</small>
          </li>
        );
      })}
    </ul>
  );
}

/** Poll interval override for testing and local verification. */
export interface DashboardProps {
  refreshIntervalMs?: number;
}

/** Render the authorized team's latest source timestamp, case counts, and overdue work. */
export function Dashboard({ refreshIntervalMs = 4_000 }: DashboardProps) {
  const query = useQuery({
    queryKey: ["reports", "dashboard", "operational"],
    queryFn: api.getOperationalDashboard,
    refetchInterval: refreshIntervalMs,
    staleTime: 1_000,
  });
  const snapshot = query.data;
  const denied =
    query.error instanceof ApiClientError &&
    [401, 403].includes(query.error.status);
  return (
    <section className={styles.dashboard} aria-label="Operational dashboard">
      <header className={styles.heading}>
        <h1>Operational dashboard</h1>
        <p>Open cases and overdue work across your team’s queues.</p>
      </header>
      {query.error ? (
        <section role="alert" className={styles.notice}>
          <h2>
            {denied
              ? "Dashboard access is restricted"
              : "Dashboard unavailable"}
          </h2>
          <p>
            {denied
              ? "This view requires access to both team cases and tasks."
              : query.error.message}
          </p>
          {!denied && (
            <button
              type="button"
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              Retry
            </button>
          )}
        </section>
      ) : query.isLoading ? (
        <p className={styles.notice} role="status">
          Loading dashboard…
        </p>
      ) : snapshot ? (
        <>
          <div className={styles.metrics}>
            <article>
              <h2>Open cases</h2>
              <strong>{snapshot.kpis.total_open_cases.toLocaleString()}</strong>
              <a href="/cases">View case register →</a>
            </article>
            <article>
              <h2>Overdue tasks</h2>
              <strong data-overdue={snapshot.kpis.total_overdue_tasks > 0}>
                {snapshot.kpis.total_overdue_tasks.toLocaleString()}
              </strong>
              <a href="/tasks">View tasks →</a>
            </article>
          </div>
          <section className={styles.panel}>
            <h2>Open cases by priority</h2>
            <PriorityBreakdown
              buckets={snapshot.kpis.open_by_priority ?? {}}
              total={snapshot.kpis.total_open_cases}
            />
          </section>
          <dl className={styles.metadata}>
            <div>
              <dt>Team queues</dt>
              <dd>{snapshot.team.join(", ") || "None assigned"}</dd>
            </div>
            <div>
              <dt>Source time</dt>
              <dd>
                <time dateTime={snapshot.generated_at}>
                  {new Date(snapshot.generated_at).toLocaleString()}
                </time>
              </dd>
            </div>
            <div>
              <dt>Age when served</dt>
              <dd>{snapshot.freshness_seconds.toLocaleString()} s</dd>
            </div>
          </dl>
        </>
      ) : null}
    </section>
  );
}

export default Dashboard;
