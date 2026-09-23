/**
 * Operational reporting endpoints (P12 / FM-051, FM-053, FM-054).
 */
import type { OperationalDashboard, OpenCasesReportResponse, OverdueTasksReportResponse, CustomReportResultResponse, ReportExportResponse } from "./types";
import { request } from "./http";

/** Fetch the current authorization-scoped operational KPI snapshot. */
export function getOperationalDashboard(): Promise<OperationalDashboard> {
  return request<OperationalDashboard>("/reports/dashboards/operational");
}

/**
 * Operational reporting (P12 / FM-051, FM-053, FM-054): team-scoped
 * snapshots with explicit ``generated_at`` source-time, custom
 * definitions, audited CSV export, and the fresh KPI dashboard.
 */
export const reports = {
  /** Open-cases snapshot for the actor's team (source-time + team scope). */
  openCases: (limit = 50) =>
    request<OpenCasesReportResponse>(`/reports/open-cases?limit=${limit}`),
  /** Overdue-tasks snapshot for the actor's team (source-time + team scope). */
  overdueTasks: (limit = 50) =>
    request<OverdueTasksReportResponse>(
      `/reports/overdue-tasks?limit=${limit}`,
    ),
  /** Render a custom report definition against the operational read models. */
  renderCustom: (definitionId: string) =>
    request<CustomReportResultResponse>(`/reports/custom/${definitionId}`),
  /** Export a custom report as audited CSV (reason recorded on AuditExported). */
  exportCustom: (definitionId: string, reason = "unspecified") =>
    request<ReportExportResponse>(
      `/reports/custom/${definitionId}/export?reason=${encodeURIComponent(reason)}`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
};
