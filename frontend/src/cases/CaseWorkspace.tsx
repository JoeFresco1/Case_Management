import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, ApiClientError } from "../api/client";
import type { CaseSummary, RedactedField } from "../api/types";

import { useState } from "react";
import { CaseList } from "./CaseList";
import { Icon } from "../shell/Icon";
import fileStyles from "./CaseFile.module.css";
import { CaseForms } from "../forms/CaseForms";
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
    queryKey: caseId
      ? caseQueryKeys.detail(caseId)
      : caseQueryKeys.detail("__missing__"),
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

function isRedacted(value: unknown): value is RedactedField {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { redacted?: unknown }).redacted === true
  );
}

function CaseDetail({ c }: { c: CaseSummary }) {
  const [params] = useSearchParams();
  const [copyState, setCopyState] = useState("");
  const sections = ["Overview", "Documents", "People", "Forms", "Workflow", "Activity"];
  const active =
    sections.find((item) => item.toLowerCase() === params.get("view")) ??
    "Overview";
  async function copyLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopyState("Link copied");
    } catch {
      setCopyState("Copy the address from your browser");
    }
  }
  const date = (value: string) =>
    new Date(value).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  const stage = c.current_workflow_state?.split("_").join(" ") ?? "Unassigned";
  return (
    <article className={fileStyles.file}>
      <header className={fileStyles.cover}>
        <div className={fileStyles.topline}>
          <Link to="/cases">← Cases</Link>
          <button onClick={copyLink}>{copyState || "Copy link"}</button>
        </div>
        <span className={fileStyles.reference}>
          <Icon name="cases" />
          {c.reference}
        </span>
        <h1>{c.title}</h1>
        <div className={fileStyles.tags}>
          <span>{c.status}</span>
          <span>{c.priority} priority</span>
          <SensitivityBadge level={c.sensitivity ?? "internal"} />
        </div>
      </header>
      <nav className={fileStyles.tabs} aria-label="Case file sections">
        {sections.map((tab) => (
          <Link
            key={tab}
            aria-current={tab === active ? "page" : undefined}
            to={tab === "Overview" ? "?" : "?view=" + tab.toLowerCase()}
          >
            {tab}
          </Link>
        ))}
      </nav>
      <div className={fileStyles.layout}>
        <div className={fileStyles.content} key={c.id + active}>
          {active === "Overview" && (
            <>
              <section>
                <h2>Case summary</h2>
                <p>{c.description || "No description recorded."}</p>
              </section>
              <section>
                <h2>Case fields</h2>
                {isRedacted(c.data) ? (
                  <p>Case fields are restricted.</p>
                ) : (
                  <dl className={styles.fields}>
                    {Object.entries(c.data ?? {}).map(([key, value]) => (
                      <div key={key}>
                        <dt>{key.split("_").join(" ")}</dt>
                        <dd>
                          {isRedacted(value)
                            ? "Restricted"
                            : typeof value === "object"
                              ? JSON.stringify(value)
                              : String(value ?? "—")}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </section>
              <section>
                <h2>Continue this case</h2>
                <div className={fileStyles.actions}>
                  <Link to="?view=documents">
                    <Icon name="cases" />
                    Review documents
                  </Link>
                  <Link to="?view=workflow">
                    <Icon name="queues" />
                    View workflow actions
                  </Link>
                </div>
              </section>
            </>
          )}
          {active === "Forms" && <CaseForms caseSummary={c} />}
          {active === "Documents" && <DocumentsPanel caseId={c.id} />}
          {active === "People" && (
            <ParticipantsPanel participants={c.participants} />
          )}
          {active === "Workflow" && <TransitionsPanel caseSummary={c} />}
          {active === "Activity" && <Timeline caseId={c.id} />}
        </div>
        <aside className={fileStyles.inspector}>
          <h2>Case properties</h2>
          <dl>
            <dt>Reference</dt>
            <dd>{c.reference}</dd>
            <dt>Case type</dt>
            <dd>{c.case_type_key.replace(/([a-z])([A-Z])/g, "$1 $2")}</dd>
            <dt>Workflow stage</dt>
            <dd className={fileStyles.stage}>{stage}</dd>
            <dt>Opened</dt>
            <dd>{date(c.created_at)}</dd>
            <dt>Last updated</dt>
            <dd>{date(c.updated_at)}</dd>
            {c.sla_state && (
              <>
                <dt>Service level</dt>
                <dd>
                  {isRedacted(c.sla_state) ? "Restricted" : c.sla_state.state}
                </dd>
              </>
            )}
            {c.sla_state &&
              !isRedacted(c.sla_state) &&
              c.sla_state.next_due_at && (
                <>
                  <dt>Next deadline</dt>
                  <dd>{date(c.sla_state.next_due_at)}</dd>
                </>
              )}
          </dl>
        </aside>
      </div>
    </article>
  );
}
