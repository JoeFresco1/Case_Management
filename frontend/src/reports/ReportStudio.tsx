import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import { defineReport } from "../api/client/reportStudio";
import { compileReportQuery, reportFields } from "./reportQuery";
import styles from "./ReportStudio.module.css";
type Definition = components["schemas"]["CustomReportDefinitionResponse"];
function message(error: unknown) {
  return error instanceof ApiClientError || error instanceof Error
    ? error.message
    : "Report operation failed.";
}

/** Author, persist, render and export reports through the tenant-scoped reporting APIs. */
export function ReportStudio() {
  const [mode, setMode] = useState("visual");
  const [name, setName] = useState("");
  const [group, setGroup] = useState("priority");
  const [filterField, setFilterField] = useState("status");
  const [filter, setFilter] = useState("");
  const [windowHours, setWindowHours] = useState("");
  const [sql, setSql] = useState(
    "SELECT priority, COUNT(*)\nFROM cases\nGROUP BY priority;",
  );
  const [definition, setDefinition] = useState<Definition | null>(null);
  const [saved, setSaved] = useState<Definition[]>([]);
  const [loadId, setLoadId] = useState("");
  const [exportReason, setExportReason] = useState("");
  const [error, setError] = useState("");
  const retry = useRef<{
    fingerprint: string;
    key: string;
    slug: string;
  } | null>(null);
  const run = useMutation({
    mutationFn: (id: string) => api.reports.renderCustom(id),
  });
  const create = useMutation({
    mutationFn: async () => {
      const spec =
        mode === "sql"
          ? compileReportQuery(sql)
          : {
              group_by: ["case." + group],
              aggregations: ["count"],
              filters: filter
                ? [{ field: "case." + filterField, op: "eq", value: filter }]
                : [],
            };
      const content = {
        name: name.trim(),
        description: "Created in Report Studio",
        ...spec,
        time_window_hours: windowHours ? Number(windowHours) : null,
      };
      const fingerprint = JSON.stringify(content);
      if (!retry.current || retry.current.fingerprint !== fingerprint)
        retry.current = {
          fingerprint,
          key: crypto.randomUUID(),
          slug: "report_" + crypto.randomUUID(),
        };
      return defineReport(
        { ...content, key: retry.current.slug },
        retry.current.key,
      );
    },
    onSuccess: (data) => {
      setDefinition(data);
      setSaved((prev) => [data, ...prev.filter((d) => d.id !== data.id)]);
      setError("");
      run.mutate(data.id);
    },
    onError: (e) => setError(message(e)),
  });
  const exportReport = useMutation({
    mutationFn: () =>
      api.reports.exportCustom(run.data!.definition_id, exportReason.trim()),
    onSuccess: (data) => {
      const blob = new Blob([data.csv_body], {
        type: "text/csv;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "report-" + data.definition_id + ".csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  return (
    <div className={styles.studio}>
      <header>
        <a href="/admin">Administration /</a>
        <h1>Report studio</h1>
        <p>
          Build grouped reports, inspect results and export a recorded snapshot.
        </p>
      </header>
      <div className={styles.layout}>
        <section className={styles.builder}>
          <h2>Report definition</h2>
          <p>
            Report definitions currently live for the API process lifetime. Copy
            the definition ID to reopen a report before that process restarts.
          </p>
          <div
            className={styles.switch}
            role="group"
            aria-label="Authoring mode"
          >
            <button
              onClick={() => setMode("visual")}
              aria-pressed={mode === "visual"}
            >
              Visual builder
            </button>
            <button
              onClick={() => setMode("sql")}
              aria-pressed={mode === "sql"}
            >
              SQL-style query
            </button>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setError("");
              run.reset();
              exportReport.reset();
              create.mutate();
            }}
          >
            <label>
              Report name
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Open records by priority"
              />
            </label>
            {mode === "visual" ? (
              <>
                <label>
                  Group records by
                  <select
                    value={group}
                    onChange={(e) => setGroup(e.target.value)}
                  >
                    {reportFields.map((f) => (
                      <option key={f} value={f}>
                        {f.replace("_", " ")}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Filter field
                  <select
                    value={filterField}
                    onChange={(e) => setFilterField(e.target.value)}
                  >
                    {reportFields.map((f) => (
                      <option key={f} value={f}>
                        {f.replace("_", " ")}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Equals
                  <input
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Leave blank for all values"
                  />
                </label>
                <p>Measure · Record count</p>
              </>
            ) : (
              <>
                <label>
                  Aggregate query
                  <textarea
                    rows={7}
                    spellCheck={false}
                    value={sql}
                    onChange={(e) => setSql(e.target.value)}
                  />
                </label>
                <details>
                  <summary>Supported query syntax</summary>
                  <p>
                    SELECT field, COUNT(*) FROM cases [WHERE field = 'value']
                    GROUP BY field
                  </p>
                  <p>
                    Fields: status, priority, queue, workflow_state. Queries
                    compile into the platform’s reporting definition and use its
                    authorization scope.
                  </p>
                </details>
              </>
            )}
            <label>
              Updated within the last hours
              <input
                type="number"
                min="1"
                max="8760"
                value={windowHours}
                onChange={(e) => setWindowHours(e.target.value)}
                placeholder="Blank for current snapshot"
              />
            </label>
            <button
              className={styles.primary}
              disabled={create.isPending || run.isPending || !name.trim()}
            >
              {create.isPending ? "Creating…" : "Create & run report"}
            </button>
          </form>
          {error && <p role="alert">{error}</p>}
          <details>
            <summary>Open an existing report</summary>
            <label>
              Definition ID
              <input
                value={loadId}
                onChange={(e) => setLoadId(e.target.value)}
              />
            </label>
            <button
              disabled={!/^[0-9a-f-]{36}$/i.test(loadId) || run.isPending}
              onClick={() => {
                setDefinition(null);
                run.mutate(loadId);
              }}
            >
              Run report
            </button>
          </details>
          {saved.length > 0 && (
            <div>
              <h3>Created this session</h3>
              {saved.map((d) => (
                <button
                  className={styles.saved}
                  key={d.id}
                  onClick={() => {
                    setDefinition(d);
                    run.mutate(d.id);
                  }}
                >
                  {d.name}
                </button>
              ))}
            </div>
          )}
        </section>
        <section className={styles.results}>
          <h2>{run.data?.definition_name ?? "Report results"}</h2><p>Scope · Open cases with open work in your team queues.</p>
          {run.isPending && <p role="status">Running report…</p>}
          {run.isError && <p role="alert">{message(run.error)}</p>}
          {!run.data && !run.isPending && (
            <div className={styles.empty}>
              <h3>Design a report to see results</h3>
              <p>
                Results come from the authorized operational data available to
                your account.
              </p>
            </div>
          )}
          {run.data && (
            <>
              <p>
                {run.data.total} {run.data.total === 1 ? "group" : "groups"} · Generated{" "}
                {new Date(run.data.generated_at).toLocaleString()}
              </p>
              <p>
                Definition ID <code>{run.data.definition_id}</code>
              </p>
              <div className={styles.table}>
                <table>
                  <thead>
                    <tr>
                      <th>{definition?.group_by.join(", ") ?? "Group"}</th>
                      <th>Records</th>
                    </tr>
                  </thead>
                  <tbody>
                    {run.data.items.map((row, i) => (
                      <tr key={i}>
                        <td>{row.key.join(" / ") || "Unassigned"}</td>
                        <td>{row.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!run.data.items.length && (
                <p>
                  No records matched this report within your reporting scope.
                </p>
              )}
              <label>
                Export reason
                <input
                  value={exportReason}
                  onChange={(e) => setExportReason(e.target.value)}
                  placeholder="Monthly operations review"
                />
              </label>
              <button
                disabled={!exportReason.trim() || exportReport.isPending}
                onClick={() => exportReport.mutate()}
              >
                Export CSV
              </button>
              {exportReport.isError && (
                <p role="alert">{message(exportReport.error)}</p>
              )}
              {exportReport.isSuccess && (
                <p role="status">
                  CSV exported · audit event {exportReport.data.audit_event_id}
                </p>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
