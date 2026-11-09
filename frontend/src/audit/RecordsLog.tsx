import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiClientError } from "../api/client";
import { Timeline } from "./Timeline";
import styles from "./RecordsLog.module.css";

/** Select authorized cases and inspect their separately authorized audit histories. */
export function RecordsLog() {
  const [offset, setOffset] = useState(0);
  const [params, setParams] = useSearchParams();
  const selected = params.get("case") ?? "";
  const setSelected = (id: string) => setParams(id ? { case: id } : {});
  const [status, setStatus] = useState("");
  const [filter, setFilter] = useState("");
  const cases = useQuery({
    queryKey: ["cases", "list", { limit: 50, offset }],
    queryFn: () => api.listCases({ limit: 50, offset }),
  });
  const rows = (cases.data?.items ?? []).filter(
    (c) =>
      (c.title + " " + c.reference)
        .toLowerCase()
        .includes(filter.trim().toLowerCase()) &&
      (!status || c.status === status),
  );
  const detail = useQuery({
    queryKey: ["cases", selected],
    queryFn: () => api.getCase(selected),
    enabled: !!selected,
    retry: false,
  });
  const current = detail.data;

  return (
    <div className={styles.page}>
      <header>
        <h1>Records log</h1>
        <p>
          Browse the records register, then select a record to inspect its
          activity history.
        </p>
      </header>
      <div className={styles.layout}>
        <section className={styles.register} aria-label="Choose a record">
          <label>
            Find on this page
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Case title or reference"
            />
          </label>
          <label>
            Status on this page
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">All statuses</option>
              {Array.from(
                new Set(cases.data?.items.map((c) => c.status) ?? []),
              ).map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          {cases.isLoading && <p>Loading records…</p>}
          {cases.isError && (
            <p role="alert">
              {cases.error instanceof ApiClientError
                ? cases.error.message
                : "Unable to load records."}
            </p>
          )}
          <div className={styles.table}>
            <table>
              <thead>
                <tr>
                  <th>Record</th>
                  <th>Status</th>
                  <th>Priority</th>
                  <th>Updated</th>
                  <th>History</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr
                    key={c.id}
                    className={selected === c.id ? styles.selected : undefined}
                  >
                    <td>
                      <a href={"/cases/" + c.id}>{c.title}</a>
                      <small>{c.reference}</small>
                    </td>
                    <td>{c.status}</td>
                    <td>{c.priority}</td>
                    <td>{new Date(c.updated_at).toLocaleDateString()}</td>
                    <td>
                      <button
                        onClick={() => setSelected(c.id)}
                        aria-pressed={selected === c.id}
                        aria-label={"View history for " + c.title}
                      >
                        View history
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {cases.data && !rows.length && (
            <p>No matching records on this page.</p>
          )}
          <nav aria-label="Record pages">
            <button
              disabled={!offset || cases.isFetching}
              onClick={() => {
                setSelected("");
                setOffset(Math.max(0, offset - 50));
              }}
            >
              Previous
            </button>
            <button
              disabled={!cases.data?.next_cursor || cases.isFetching}
              onClick={() => {
                setSelected("");
                setOffset(offset + 50);
              }}
            >
              Next
            </button>
          </nav>
        </section>
        <section className={styles.activity}>
          {detail.isLoading && selected ? (
            <p role="status">Loading selected record…</p>
          ) : detail.isError ? (
            <p role="alert">
              {detail.error instanceof ApiClientError
                ? detail.error.message
                : "Unable to load the selected record."}
            </p>
          ) : current ? (
            <>
              <header>
                <small>{current.reference}</small>
                <h2>{current.title}</h2>
                <a href={"/cases/" + current.id}>Open case file →</a>
              </header>
              <Timeline key={current.id} caseId={current.id} />
            </>
          ) : (
            <div className={styles.empty}>
              <h2>Review a record’s history</h2>
              <p>
                Choose a record to see its events, dates and actor details.
                Access is checked for each case.
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
