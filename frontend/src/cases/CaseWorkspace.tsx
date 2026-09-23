import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api, ApiClientError } from "../api/client";
import type { CaseSummary, RedactedField } from "../api/types";

import { LabelHistory, SYNTHETIC_LABEL_HISTORY } from "./LabelHistory";
import { DocumentsPanel } from "./DocumentsPanel";
import { ParticipantsPanel } from "./ParticipantsPanel";
import { TransitionsPanel } from "./TransitionsPanel";
import { SensitivityBadge } from "../security/Sensitivity";
import { Timeline } from "../audit/Timeline";
import styles from "./CaseWorkspace.module.css";

/**
 * TanStack Query keys for the case surface (P01-US2 / FR-002).
 *
 * Centralized so invalidation in one place — e.g. after a workflow
 * transition or a doc upload — propagates everywhere. The shape is
 * `[cases, ...]` for list pages and `[cases, id]` for the workspace.
 */
export const caseQueryKeys = {
  all: ["cases"] as const,
  list: (params: { limit?: number; offset?: number } = {}) =>
    ["cases", "list", params] as const,
  detail: (caseId: string) => ["cases", caseId] as const,
};

export function CaseWorkspace() {
  const params = useParams<{ caseId?: string }>();
  const caseId = params.caseId;

  const caseQuery = useQuery<CaseSummary>({
    queryKey: caseId ? caseQueryKeys.detail(caseId) : caseQueryKeys.detail("__missing__"),
    queryFn: () => {
      if (!caseId) throw new Error("missing caseId");
      return api.getCase(caseId);
    },
    enabled: !!caseId,
    staleTime: 30_000,
  });

  if (!caseId) {
    return <CaseList />;
  }

  if (caseQuery.isLoading) return <p>Loading case…</p>;
  if (caseQuery.error) {
    return (
      <p className={styles.error}>
        {caseQuery.error instanceof ApiClientError
          ? caseQuery.error.message
          : "Failed to load case."}
      </p>
    );
  }
  if (!caseQuery.data) return <p>Case not found.</p>;
  return <CaseDetail c={caseQuery.data} />;
}

function CaseList() {
  const listQuery = useQuery({
    queryKey: caseQueryKeys.list({ limit: 50 }),
    queryFn: () => api.listCases({ limit: 50 }),
    staleTime: 30_000,
  });

  if (listQuery.isLoading) return <p>Loading cases…</p>;
  if (listQuery.error) {
    return (
      <p className={styles.error}>
        {listQuery.error instanceof ApiClientError
          ? listQuery.error.message
          : "Failed to load cases."}
      </p>
    );
  }
  const cases = listQuery.data?.items ?? [];
  if (cases.length === 0) {
    return (
      <p className={styles.empty}>
        No cases yet. The seed script should have created one. Run
        <code> uv run python -m app.cli seed</code>.
      </p>
    );
  }
  return (
    <ul className={styles.list}>
      {cases.map((c) => (
        <li key={c.id} className={styles.listItem}>
          <a href={`/cases/${c.id}`}>
            <strong>{c.reference}</strong> — {c.title}
          </a>
          <div className={styles.meta}>
            <span>{c.status}</span> · <span>{c.priority}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function isRedacted(value: unknown): value is RedactedField {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { redacted?: unknown }).redacted === true
  );
}

function RedactedSlot({ slot }: { slot: "assignee" | "queue" | "sla_state" }) {
  return (
    <p className={styles.empty}>
      <em>{slot}</em>: restricted
    </p>
  );
}

function CaseDetail({ c }: { c: CaseSummary }) {
  return (
    <article className={styles.detail}>
      <header>
        <h1>{c.title}</h1>
        <div className={styles.meta}>
          <span>{c.reference}</span> · <span>{c.status}</span> ·{" "}
          <span>{c.priority}</span> · <span>{c.case_type_key}</span> ·{" "}
          <SensitivityBadge level={c.sensitivity ?? "internal"} />
        </div>
      </header>
      <section>
        <h2>Description</h2>
        <p>{c.description || <em>(none)</em>}</p>
      </section>
      <section>
        <h2>Structured data</h2>
        {isRedacted(c.data) ? (
          <p className={styles.empty}>Structured data is restricted.</p>
        ) : (
          <pre className={styles.data}>{JSON.stringify(c.data, null, 2)}</pre>
        )}
      </section>
      <section>
        <h2>Field label history</h2>
        <LabelHistory
          fieldKey="title"
          history={SYNTHETIC_LABEL_HISTORY}
          currentLabel={c.title}
        />
      </section>
      <section>
        <h2>Workflow</h2>
        <p>
          Current state:{" "}
          <strong>{c.current_workflow_state ?? "(none yet)"}</strong>
        </p>
        <RedactedSlot slot="assignee" />
        <RedactedSlot slot="queue" />
        <RedactedSlot slot="sla_state" />
      </section>
      <TransitionsPanel caseSummary={c} />
      <ParticipantsPanel participants={c.participants} />
      <section>
        <h2>Open tasks</h2>
        <p className={styles.empty}>
          Tasks panel arrives in Phase 2.
        </p>
      </section>
      <section>
        <DocumentsPanel caseId={c.id} />
      </section>
      <section>
        <h2>Recent decisions</h2>
        <p className={styles.empty}>
          Decisions panel arrives in P02.
        </p>
      </section>
      <Timeline caseId={c.id} />
    </article>
  );
}
