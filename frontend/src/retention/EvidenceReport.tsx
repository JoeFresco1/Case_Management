import { useQuery } from "@tanstack/react-query";

import { ApiClientError } from "../api/client";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Defensible-deletion evidence report (FM-042 / P08-US6 / P12-US9).
 *
 * Renders the per-case chain-of-custody packet returned by
 * ``GET /api/v1/retention/evidence/{case_id}``:
 *
 * - The case identity + the report's ``generated_at`` timestamp.
 * - Whether the case has been disposed (at least one approved
 *   disposition) — the compliance signal. A case with
 *   ``disposed=false`` is rendered with an explicit "no silent
 *   deletion" marker so the auditor can confirm the record is
 *   absent from the platform-wide defensible-deletion log.
 * - The published retention policy that authorised the disposition
 *   (id, version number, published_at, published_by).
 * - Every disposition decision (pending / approved / rejected) in
 *   chronological order so the full history is visible.
 * - The bounded audit chain of disposition-related event types so
 *   the chain of custody is re-walkable.
 *
 * The component is read-only; it never calls a mutation endpoint.
 * Wire shapes are generated from the OpenAPI contract; the local
 * types are kept narrow and explicit so the auditor's scanline is
 * not cluttered by optional fields.
 */

export interface PolicyEvidenceEntry {
  policy_id: string;
  category: "case" | "document" | "audit";
  target_key: string;
  period_days: number;
  version_number: number;
  status: "draft" | "published" | "retired";
  published_at: string | null;
  published_by: string | null;
}

export interface DispositionEvidenceEntry {
  request_id: string;
  category: "case" | "document" | "audit";
  status: "pending" | "approved" | "rejected";
  surfaced_at: string;
  surfaced_by: string | null;
  decided_at: string | null;
  decided_by: string | null;
  rationale: string | null;
}

export interface AuditChainEntryResponse {
  event_id: string;
  event_type: string;
  actor_id: string;
  actor_type: string;
  subject_type: string;
  subject_id: string;
  occurred_at: string;
  correlation_id: string;
  summary: string;
}

export interface EvidenceReport {
  case_id: string;
  case_reference: string;
  case_created_at: string;
  tenant_id: string;
  kind: "snapshot";
  generated_at: string;
  actor_id: string;
  disposed: boolean;
  policy: PolicyEvidenceEntry | null;
  dispositions: DispositionEvidenceEntry[];
  audit_chain: AuditChainEntryResponse[];
}

async function fetchEvidenceReport(
  caseId: string,
  actorId: string,
  tenantId: string,
): Promise<EvidenceReport> {
  const response = await fetch(
    `/api/v1/retention/evidence/${encodeURIComponent(caseId)}`,
    {
      headers: {
        "X-Correlation-Id": crypto.randomUUID(),
        "X-Dev-User": actorId,
        "X-Dev-Tenant": tenantId,
      },
    },
  );
  if (!response.ok) {
    let body: { message?: string } = {};
    try {
      body = await response.json();
    } catch {
      // ignore parse errors
    }
    throw new ApiClientError(
      body.message ?? `evidence report fetch failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as EvidenceReport;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

function ReportHeader(props: { report: EvidenceReport }) {
  return (
    <header className={styles.header}>
      <h2>
        Defensible-deletion evidence · {props.report.case_reference}
      </h2>
      <p className={styles.muted}>
        Snapshot as of {formatTimestamp(props.report.generated_at)} ·{" "}
        {props.report.disposed
          ? "DISPOSED — at least one approved disposition"
          : "NOT DISPOSED — no approved disposition on file"}
      </p>
    </header>
  );
}

function PolicyCard(props: { policy: PolicyEvidenceEntry | null }) {
  if (props.policy === null) {
    return (
      <section className={styles.workspace}>
        <h3>Authorising policy</h3>
        <p className={styles.muted}>
          No approved disposition — the policy that would have authorised
          disposition is not part of the report.
        </p>
      </section>
    );
  }
  const p = props.policy;
  return (
    <section className={styles.workspace}>
      <h3>Authorising policy</h3>
      <dl>
        <dt>Policy id</dt>
        <dd>
          <code>{p.policy_id}</code>
        </dd>
        <dt>Category</dt>
        <dd>{p.category}</dd>
        <dt>Target key</dt>
        <dd>{p.target_key === "" ? "(tenant default)" : p.target_key}</dd>
        <dt>Version</dt>
        <dd>
          v{p.version_number} · {p.status} · {p.period_days} day period
        </dd>
        <dt>Published</dt>
        <dd>
          {formatTimestamp(p.published_at)} by{" "}
          <code>{p.published_by ?? "—"}</code>
        </dd>
      </dl>
    </section>
  );
}

function DispositionsTable(props: { rows: DispositionEvidenceEntry[] }) {
  if (props.rows.length === 0) {
    return (
      <section className={styles.workspace}>
        <h3>Disposition history</h3>
        <p className={styles.muted}>
          No disposition requests on file. The case has not been surfaced
          for retention review.
        </p>
      </section>
    );
  }
  return (
    <section className={styles.workspace}>
      <h3>Disposition history</h3>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Request</th>
            <th>Status</th>
            <th>Surfaced</th>
            <th>Surfaced by</th>
            <th>Decided</th>
            <th>Decided by</th>
            <th>Rationale</th>
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row) => (
            <tr key={row.request_id}>
              <td>
                <code>{row.request_id.slice(0, 8)}</code>
              </td>
              <td>{row.status}</td>
              <td>{formatTimestamp(row.surfaced_at)}</td>
              <td>
                <code>{row.surfaced_by ?? "—"}</code>
              </td>
              <td>{formatTimestamp(row.decided_at)}</td>
              <td>
                <code>{row.decided_by ?? "—"}</code>
              </td>
              <td>{row.rationale ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function AuditChainTable(props: { entries: AuditChainEntryResponse[] }) {
  if (props.entries.length === 0) {
    return (
      <section className={styles.workspace}>
        <h3>Audit chain</h3>
        <p className={styles.muted}>
          No disposition-related audit rows on file for this case.
        </p>
      </section>
    );
  }
  return (
    <section className={styles.workspace}>
      <h3>Audit chain</h3>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Occurred</th>
            <th>Event</th>
            <th>Actor</th>
            <th>Subject</th>
            <th>Summary</th>
            <th>Correlation</th>
          </tr>
        </thead>
        <tbody>
          {props.entries.map((entry) => (
            <tr key={entry.event_id}>
              <td>{formatTimestamp(entry.occurred_at)}</td>
              <td>
                <code>{entry.event_type}</code>
              </td>
              <td>
                <code>
                  {entry.actor_id.slice(0, 8)} ({entry.actor_type})
                </code>
              </td>
              <td>
                <code>
                  {entry.subject_type}:{entry.subject_id.slice(0, 8)}
                </code>
              </td>
              <td>{entry.summary}</td>
              <td>
                <code>{entry.correlation_id.slice(0, 8)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export interface EvidenceReportViewProps {
  caseId: string;
  actorId?: string;
  tenantId?: string;
}

export function EvidenceReportView(props: EvidenceReportViewProps) {
  const actorId = props.actorId ?? "compliance";
  const tenantId = props.tenantId ?? "00000000-0000-0000-0000-000000000000";

  const query = useQuery({
    queryKey: ["retention", "evidence", props.caseId],
    queryFn: () => fetchEvidenceReport(props.caseId, actorId, tenantId),
    enabled: Boolean(props.caseId),
    staleTime: 15_000,
  });

  if (query.isLoading) {
    return <p>Loading evidence report…</p>;
  }
  if (query.error) {
    return <p className={styles.error}>{query.error.message}</p>;
  }
  if (!query.data) {
    return <p className={styles.muted}>No evidence report available.</p>;
  }

  const report = query.data;
  return (
    <article className={styles.workspace}>
      <ReportHeader report={report} />
      <PolicyCard policy={report.policy} />
      <DispositionsTable rows={report.dispositions} />
      <AuditChainTable entries={report.audit_chain} />
    </article>
  );
}

export default EvidenceReportView;
