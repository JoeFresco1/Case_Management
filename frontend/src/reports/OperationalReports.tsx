import { useQuery } from "@tanstack/react-query";

import {
  api,
  type OpenCasesReportResponse,
  type OverdueTasksReportResponse,
} from "../api/client";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Supervisor operational reports (P12-US1, US2 / FM-051).
 *
 * Two snapshots render in this component, both team-scoped to the
 * queue memberships the actor currently holds:
 *
 * - Open cases by status  (``GET /api/v1/reports/open-cases``)
 * - Overdue tasks         (``GET /api/v1/reports/overdue-tasks``)
 *
 * Each report carries its ``generated_at`` source-time plus the
 * ``team`` authorization scope so the supervisor knows exactly when
 * the system of record was sampled (P12 §1) and which queues the
 * numbers cover. Reports are read-only; the component does not call
 * any mutation endpoint. Wire shapes are generated from OpenAPI via
 * ``src/api/client/`` — no fields are typed by hand here.
 */

function formatAging(overdueSeconds: number): string {
  if (overdueSeconds <= 0) return "on time";
  const hours = Math.floor(overdueSeconds / 3600);
  const minutes = Math.floor((overdueSeconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes.toString().padStart(2, "0")}m overdue`;
  if (minutes > 0) return `${minutes}m overdue`;
  return `${overdueSeconds}s overdue`;
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString();
}

function ReportHeader(props: {
  title: string;
  generatedAt: string;
  team: string[];
  total: number;
}) {
  return (
    <header className={styles.header}>
      <h2>{props.title}</h2>
      <p className={styles.muted}>
        {props.total} {props.total === 1 ? "row" : "rows"} · team:{" "}
        {props.team.length === 0 ? "(none)" : props.team.join(", ")} · as of{" "}
        {formatTimestamp(props.generatedAt)}
      </p>
    </header>
  );
}

function OpenCasesTable(props: { report: OpenCasesReportResponse }) {
  if (props.report.items.length === 0) {
    return (
      <p className={styles.empty}>
        No open cases on this team. The supervisor queue is clear.
      </p>
    );
  }
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Reference</th>
          <th>Title</th>
          <th>Status</th>
          <th>Priority</th>
          <th>Workflow state</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {props.report.items.map((row) => (
          <tr key={row.case_id}>
            <td>
              <a href={`/cases/${row.case_id}`}>{row.reference}</a>
            </td>
            <td>{row.title}</td>
            <td>{row.status}</td>
            <td>{row.priority}</td>
            <td>
              {row.current_workflow_state ? (
                <code>{row.current_workflow_state}</code>
              ) : (
                "—"
              )}
            </td>
            <td>{formatTimestamp(row.updated_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OverdueTasksTable(props: { report: OverdueTasksReportResponse }) {
  if (props.report.items.length === 0) {
    return (
      <p className={styles.empty}>
        No overdue tasks on this team. SLA windows are clean.
      </p>
    );
  }
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Title</th>
          <th>Case</th>
          <th>Priority</th>
          <th>Queue</th>
          <th>Due</th>
          <th>Aging</th>
        </tr>
      </thead>
      <tbody>
        {props.report.items.map((row) => (
          <tr key={row.task_id} className={styles.errorRow}>
            <td>{row.title}</td>
            <td>
              <a href={`/cases/${row.case_id}`}>
                <code>{row.case_id.slice(0, 8)}</code>
              </a>
            </td>
            <td>{row.priority}</td>
            <td>{row.assignee_queue_id ?? "—"}</td>
            <td>{formatTimestamp(row.due_at)}</td>
            <td className={styles.error}>{formatAging(row.overdue_seconds)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export interface OperationalReportsProps {
  /** Optional override of the limit query parameter. */
  limit?: number;
}

export function OperationalReports(props: OperationalReportsProps) {
  const limit = props.limit ?? 50;

  const openCasesQuery = useQuery({
    queryKey: ["reports", "open-cases", { limit }],
    queryFn: () => api.reports.openCases(limit),
    staleTime: 15_000,
  });

  const overdueQuery = useQuery({
    queryKey: ["reports", "overdue-tasks", { limit }],
    queryFn: () => api.reports.overdueTasks(limit),
    staleTime: 15_000,
  });

  return (
    <section className={styles.workspace}>
      <ReportHeader
        title="Open cases by status (team)"
        generatedAt={
          openCasesQuery.data?.generated_at ?? new Date().toISOString()
        }
        team={openCasesQuery.data?.team ?? []}
        total={openCasesQuery.data?.total ?? 0}
      />
      {openCasesQuery.isLoading ? (
        <p>Loading open cases…</p>
      ) : openCasesQuery.error ? (
        <p className={styles.error}>{openCasesQuery.error.message}</p>
      ) : openCasesQuery.data ? (
        <OpenCasesTable report={openCasesQuery.data} />
      ) : null}

      <ReportHeader
        title="Overdue tasks (team)"
        generatedAt={overdueQuery.data?.generated_at ?? new Date().toISOString()}
        team={overdueQuery.data?.team ?? []}
        total={overdueQuery.data?.total ?? 0}
      />
      {overdueQuery.isLoading ? (
        <p>Loading overdue tasks…</p>
      ) : overdueQuery.error ? (
        <p className={styles.error}>{overdueQuery.error.message}</p>
      ) : overdueQuery.data ? (
        <OverdueTasksTable report={overdueQuery.data} />
      ) : null}
    </section>
  );
}

export default OperationalReports;