import { useState } from "react";

import {
  ApiClientError,
  api,
  type QuarantineRecordResponse,
} from "../api/client";
import styles from "../cases/CaseWorkspace.module.css";

/**
 * Operator console for the quarantine queue (P04-US5 / FM-027, D-6).
 *
 * Renders the list of held quarantine records fetched from
 * ``GET /api/v1/admin/quarantine`` and offers per-row actions to
 * release or reject a record. The underlying use cases
 * (``POST /api/v1/admin/quarantine/{id}/release`` and
 * ``POST .../reject``) emit audit + outbox events that surface in the
 * case timeline.
 *
 * # Synthetic posture
 *
 * The component never embeds row payload data — only ``source_label``
 * and ``channel_key`` plus the diagnostic ``context`` keys. Operators
 * triage from the diagnostic context, not from re-fetched payload
 * bytes (Constitution V).
 *
 * # Wire discipline
 *
 * Records are the generated ``QuarantineRecordResponse`` reached
 * through the shared ``api.quarantine`` client; no local ``interface``
 * re-declares the wire. Detail expansion reuses the listed record (no
 * second fetch) and mutations send ``Idempotency-Key`` from the client.
 */

/**
 * Render diagnostic context as ``key=value`` pairs.
 *
 * Only context *keys* plus scalar summaries are shown; the captured
 * payload bytes are never part of the context contract, so nothing
 * sensitive can render here even when the backend adds new keys.
 */
function formatContext(
  context: Record<string, unknown> | null | undefined,
): string {
  if (context == null) {
    return "—";
  }
  const entries = Object.entries(context);
  if (entries.length === 0) {
    return "—";
  }
  return entries
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
}

/** Render an API failure as operator text (safe-only contract). */
function toErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError) {
    return err.message;
  }
  return err instanceof Error ? err.message : fallback;
}

export function Quarantine() {
  const [items, setItems] = useState<QuarantineRecordResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  async function refresh() {
    setError(null);
    setBusy(true);
    try {
      const result = await api.quarantine.list({ status: "held", limit: 50 });
      setItems(result.items);
      setTotal(result.total);
    } catch (err) {
      // why keep items: a failed refresh must not wipe the triage list
      // the operator is working through.
      setError(toErrorMessage(err, "inspect failed"));
    } finally {
      setBusy(false);
    }
  }

  async function resolve(
    recordId: string,
    action: "release" | "reject",
    reason: string,
  ) {
    setPending(recordId);
    setError(null);
    try {
      if (action === "release") {
        await api.quarantine.release(recordId, reason);
      } else {
        await api.quarantine.reject(recordId, reason);
      }
      await refresh();
    } catch (err) {
      // why keep items + expandedId: an already-resolved (422) record
      // still shows its place in the list so the operator sees what the
      // server reported without losing selection.
      setError(toErrorMessage(err, `${action} failed`));
    } finally {
      setPending(null);
    }
  }

  function toggleDetail(recordId: string) {
    setExpandedId((current) => (current === recordId ? null : recordId));
  }

  return (
    <section>
      <h2>Quarantine (P04-US5 / FM-027)</h2>
      <p className={styles.meta}>
        Captured items that failed channel validation. Release to
        re-process with the (possibly corrected) row, or reject to drop
        permanently. Synthetic fixture only — <code>example.test</code>
        records.
      </p>
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
        <button type="button" disabled={busy} onClick={refresh}>
          {busy ? "Refreshing…" : "Refresh queue"}
        </button>
        <input
          type="text"
          placeholder="Resolution note (optional)"
          aria-label="Resolution note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={500}
          style={{ flex: 1 }}
        />
      </div>
      {error ? <p className={styles.error}>{error}</p> : null}
      <p className={styles.meta}>
        {total} held record(s).
      </p>
      {items.length === 0 ? (
        <p className={styles.meta}>No held records.</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Channel</th>
              <th>Source</th>
              <th>Error</th>
              <th>Context</th>
              <th>Captured</th>
              <th>Correlation</th>
              <th>Detail</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {items.flatMap((record) => {
              const expanded = expandedId === record.id;
              const mainRow = (
                <tr key={record.id}>
                  <td>
                    <code>{record.channel_key}</code>
                  </td>
                  <td>{record.source_label}</td>
                  <td>
                    <span className={styles.error}>
                      {record.error_code}
                    </span>
                    <br />
                    <small>{record.error_message}</small>
                  </td>
                  <td>
                    <code>{formatContext(record.context)}</code>
                  </td>
                  <td>
                    <small>{record.captured_at}</small>
                  </td>
                  <td>
                    <code>{record.correlation_id}</code>
                  </td>
                  <td>
                    <button
                      type="button"
                      aria-label={
                        expanded
                          ? `Hide detail for ${record.id}`
                          : `Show detail for ${record.id}`
                      }
                      onClick={() => toggleDetail(record.id)}
                    >
                      {expanded ? "Hide" : "Detail"}
                    </button>
                  </td>
                  <td>
                    <button
                      type="button"
                      disabled={pending === record.id}
                      onClick={() => resolve(record.id, "release", note)}
                    >
                      {pending === record.id ? "…" : "Release"}
                    </button>
                    <button
                      type="button"
                      disabled={pending === record.id}
                      onClick={() => resolve(record.id, "reject", note)}
                    >
                      {pending === record.id ? "…" : "Reject"}
                    </button>
                  </td>
                </tr>
              );
              // why same record: the list response already carries the
              // full resolution fields, so expansion is a local toggle —
              // no second fetch, no payload bytes involved.
              const detailRow = expanded ? (
                <tr key={`${record.id}-detail`}>
                  <td colSpan={8}>
                    <dl>
                      <dt>Record id</dt>
                      <dd>
                        <code>{record.id}</code>
                      </dd>
                      <dt>Tenant id</dt>
                      <dd>
                        <code>{record.tenant_id}</code>
                      </dd>
                      <dt>Channel id</dt>
                      <dd>
                        <code>{record.channel_id}</code>
                      </dd>
                      <dt>Captured by</dt>
                      <dd>{record.captured_by}</dd>
                      <dt>Status</dt>
                      <dd>
                        <code>{record.status}</code>
                      </dd>
                      <dt>Resolved at</dt>
                      <dd>{record.resolved_at ?? "—"}</dd>
                      <dt>Resolved by</dt>
                      <dd>{record.resolved_by ?? "—"}</dd>
                      <dt>Resolution note</dt>
                      <dd>{record.resolution_note ?? "—"}</dd>
                    </dl>
                  </td>
                </tr>
              ) : null;
              return detailRow ? [mainRow, detailRow] : [mainRow];
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default Quarantine;
