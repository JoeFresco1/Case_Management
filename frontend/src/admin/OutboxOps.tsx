import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Operator console for background work and platform operability
 * (G-6: FM-052 failed-work/outbox operations, FM-055 versioned event
 * catalog, FM-056 outbound webhooks, FM-047 extension catalog overlay,
 * T7 system diagnostics + storage view).
 *
 * Four sections, each with an independent query so one failing seam
 * never blanks the others:
 *
 * 1. **Failed work** — lag counters plus one row per pending outbox
 *    entry with `event_type`, `attempt_count`, and the operator-safe
 *    `last_error`. Replay is explicit two-step (arm, then confirm):
 *    the reset only re-arms the row for the next dispatcher tick, it
 *    never edits case data. The response distinguishes `reset` from
 *    `no-op` (already dispatched) so the operator sees intent outcome.
 * 2. **Event catalog compatibility** — the built-in catalog beside the
 *    extension-union catalog. Entries are additive-only: an extension
 *    entry that narrows a built-in `(event_type, version)` payload is
 *    flagged as breaking and must ship as a new version instead.
 * 3. **Webhook subscriptions** — tenant subscriptions with lifecycle
 *    `status` plus a confirmed replay of dead-lettered deliveries per
 *    subscription (202, `Idempotency-Key` required by the contract).
 * 4. **Diagnostics & config impact** — desired (tenant config) vs
 *    actual (process) background services, declared vs actual storage,
 *    and the synthetic-environment marker. Replays and setting changes
 *    are audited; this view only reads.
 *
 * Every wire shape below aliases the generated OpenAPI schemas —
 * nothing is hand-typed, so contract drift surfaces at typecheck
 * time. Errors render the server's safe `message` only (never stack
 * traces, SQL, payloads, or secret values).
 */

/** Generated outbox contracts (FM-052) — aliased, never re-declared. */
export type OutboxListResponse = components["schemas"]["OutboxListResponse"];
export type OutboxReplayResponse =
  components["schemas"]["OutboxReplayResponse"];
export type OutboxRow = components["schemas"]["OutboxRowResponse"];
/** Generated event-catalog contracts (FM-055 / FM-047). */
export type EventTypeSpec = components["schemas"]["EventTypeOut"];
export type ExtensionEventCatalog =
  components["schemas"]["ExtensionEventCatalog"];
/** Generated webhook + diagnostics contracts (FM-056 / T7). */
export type SubscriptionListResponse =
  components["schemas"]["SubscriptionListResponse"];
export type WebhookSubscription = components["schemas"]["SubscriptionResponse"];
export type DiagnosticsResponse = components["schemas"]["DiagnosticsResponse"];
export type StorageResponse = components["schemas"]["StorageResponse"];

/**
 * Built-in catalog envelope.
 *
 * Why hand-written: `GET /api/v1/events/catalog` has no generated
 * response schema (it returns an untyped dict), so the envelope shape
 * is documented here while every *entry* reuses the generated
 * `EventTypeOut`. The extension-union endpoint does have a schema
 * (`ExtensionEventCatalog`) and is used verbatim.
 */
export interface BuiltinEventCatalog {
  items: EventTypeSpec[];
  next_cursor: null;
  limit: number;
  source: string;
}

/** One catalog entry tagged with the registry that contributed it. */
export interface CatalogEntry extends EventTypeSpec {
  source: "built-in" | "extension";
}

/** An extension entry that narrows a built-in version's payload keys. */
export interface BreakingCatalogChange {
  event_type: string;
  version: number;
  removed_keys: string[];
}

/**
 * Merge the built-in catalog with the extension overlay.
 *
 * Built-in entries win on `(event_type, version)` conflicts (the
 * server resolves the union the same way), so extension duplicates
 * never shadow the platform contract in this view.
 */
export function unionCatalog(
  builtin: EventTypeSpec[],
  extended: EventTypeSpec[],
): CatalogEntry[] {
  const seen = new Set(
    builtin.map((entry) => `${entry.event_type}@${entry.version}`),
  );
  const merged: CatalogEntry[] = builtin.map((entry) => ({
    ...entry,
    source: "built-in",
  }));
  for (const entry of extended) {
    const key = `${entry.event_type}@${entry.version}`;
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push({ ...entry, source: "extension" });
  }
  return merged;
}

/**
 * Find additive-only violations: an extension entry reusing a
 * built-in `(event_type, version)` while dropping payload keys.
 * Removal requires a new version (P13 Q2) — flag, never auto-fix.
 */
export function findBreakingCatalogChanges(
  builtin: EventTypeSpec[],
  extended: EventTypeSpec[],
): BreakingCatalogChange[] {
  const builtinByKey = new Map(
    builtin.map((entry) => [`${entry.event_type}@${entry.version}`, entry]),
  );
  const breaking: BreakingCatalogChange[] = [];
  for (const entry of extended) {
    const base = builtinByKey.get(`${entry.event_type}@${entry.version}`);
    if (!base) continue;
    const removed = base.payload_keys.filter(
      (key) => !entry.payload_keys.includes(key),
    );
    if (removed.length > 0) {
      breaking.push({
        event_type: entry.event_type,
        version: entry.version,
        removed_keys: removed,
      });
    }
  }
  return breaking;
}

/** Local dev identity headers (mirrors the existing outbox console). */
function devHeaders(idempotencyKey?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "X-Correlation-Id": crypto.randomUUID(),
    "X-Dev-User": "operator",
    "X-Dev-Tenant": "00000000-0000-0000-0000-000000000000",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return headers;
}

/**
 * Raise the server's safe error message (never stack/SQL/secrets —
 * the backend `ErrorResponse` envelope only carries those).
 */
async function throwForStatus(
  response: Response,
  fallback: string,
): Promise<never> {
  let body: { message?: string } = {};
  try {
    body = await response.json();
  } catch {
    // ignore parse errors — fall back to the status line
  }
  throw new ApiClientError(
    body.message ?? `${fallback}: ${response.status}`,
    response.status,
  );
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: devHeaders() });
  if (!response.ok) {
    await throwForStatus(response, "request failed");
  }
  return (await response.json()) as T;
}

async function fetchOutbox(limit: number): Promise<OutboxListResponse> {
  return getJson<OutboxListResponse>(`/api/v1/admin/outbox?limit=${limit}`);
}

async function replayOutboxRow(rowId: string): Promise<OutboxReplayResponse> {
  const response = await fetch(`/api/v1/admin/outbox/${rowId}/replay`, {
    method: "POST",
    headers: devHeaders(),
  });
  if (!response.ok) {
    await throwForStatus(response, "replay failed");
  }
  return (await response.json()) as OutboxReplayResponse;
}

async function fetchBuiltinCatalog(): Promise<BuiltinEventCatalog> {
  return getJson<BuiltinEventCatalog>("/api/v1/events/catalog");
}

async function fetchExtensionCatalog(): Promise<ExtensionEventCatalog> {
  return getJson<ExtensionEventCatalog>("/api/v1/extensions/events/catalog");
}

async function fetchSubscriptions(): Promise<SubscriptionListResponse> {
  return getJson<SubscriptionListResponse>("/api/v1/webhooks/subscriptions");
}

async function replaySubscriptionDeliveries(
  subscriptionId: string,
): Promise<WebhookSubscription> {
  const response = await fetch(
    `/api/v1/webhooks/subscriptions/${subscriptionId}/replay`,
    { method: "POST", headers: devHeaders(crypto.randomUUID()) },
  );
  if (!response.ok) {
    await throwForStatus(response, "subscription replay failed");
  }
  return (await response.json()) as WebhookSubscription;
}

async function fetchDiagnostics(): Promise<DiagnosticsResponse> {
  return getJson<DiagnosticsResponse>("/api/v1/admin/system/diagnostics");
}

async function fetchStorage(): Promise<StorageResponse> {
  return getJson<StorageResponse>("/api/v1/admin/system/storage");
}

/** Render one unknown config value without ever dumping nested secrets. */
function formatConfigValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "on" : "off";
  if (Array.isArray(value)) return `${value.length} entr${value.length === 1 ? "y" : "ies"}`;
  if (typeof value === "object") return `${Object.keys(value).length} keys`;
  return "—";
}

function errorMessage(error: unknown): string {
  return error instanceof ApiClientError
    ? error.message
    : "Request failed. Retry or check the operator runbook.";
}

export function OutboxOps() {
  const queryClient = useQueryClient();
  const [confirmRowId, setConfirmRowId] = useState<string | null>(null);
  const [confirmSubscriptionId, setConfirmSubscriptionId] = useState<
    string | null
  >(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const outboxQuery = useQuery({
    queryKey: ["admin", "outbox", { limit: 50 }],
    queryFn: () => fetchOutbox(50),
    refetchInterval: 5_000,
  });
  const builtinCatalogQuery = useQuery({
    queryKey: ["events", "catalog"],
    queryFn: fetchBuiltinCatalog,
  });
  const extensionCatalogQuery = useQuery({
    queryKey: ["extensions", "events", "catalog"],
    queryFn: fetchExtensionCatalog,
  });
  const subscriptionsQuery = useQuery({
    queryKey: ["admin", "webhook-subscriptions"],
    queryFn: fetchSubscriptions,
  });
  const diagnosticsQuery = useQuery({
    queryKey: ["admin", "system", "diagnostics"],
    queryFn: fetchDiagnostics,
  });
  const storageQuery = useQuery({
    queryKey: ["admin", "system", "storage"],
    queryFn: fetchStorage,
  });

  const replayRow = useMutation({
    mutationFn: replayOutboxRow,
    onSuccess: (data, rowId) => {
      setConfirmRowId(null);
      setActionError(null);
      setNotice(
        data.reset
          ? `Replay armed for row ${rowId}: the dispatcher will re-attempt it on the next tick.`
          : `Replay for row ${rowId} was a no-op: the row was already dispatched.`,
      );
      queryClient.invalidateQueries({ queryKey: ["admin", "outbox"] });
    },
    onError: (error: unknown) => {
      setConfirmRowId(null);
      setNotice(null);
      setActionError(errorMessage(error));
    },
  });

  const replaySubscription = useMutation({
    mutationFn: replaySubscriptionDeliveries,
    onSuccess: (data) => {
      setConfirmSubscriptionId(null);
      setActionError(null);
      setNotice(
        `Dead-lettered deliveries replay requested for ${data.description || data.url}. ` +
          `The dispatcher will re-attempt them; the request is audited.`,
      );
      queryClient.invalidateQueries({
        queryKey: ["admin", "webhook-subscriptions"],
      });
    },
    onError: (error: unknown) => {
      setConfirmSubscriptionId(null);
      setNotice(null);
      setActionError(errorMessage(error));
    },
  });

  const builtinItems = builtinCatalogQuery.data?.items ?? [];
  const extensionItems = extensionCatalogQuery.data?.items ?? [];
  const catalog = unionCatalog(builtinItems, extensionItems);
  const breaking = findBreakingCatalogChanges(builtinItems, extensionItems);

  return (
    <section aria-labelledby="ops-heading">
      <h2 id="ops-heading">Operations</h2>
      <p className={styles.meta}>
        Failed work, event compatibility, webhook state, and diagnostics.
        Replays re-arm retry state only — they never edit case data — and
        subscription replays are audited.
      </p>
      {notice ? <p aria-live="polite">{notice}</p> : null}
      {actionError ? (
        <p role="alert" className={styles.error}>
          {actionError}
        </p>
      ) : null}

      <h3 id="ops-failed-work">Failed work</h3>
      {outboxQuery.isLoading ? <p>Loading outbox…</p> : null}
      {outboxQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(outboxQuery.error)}
        </p>
      ) : null}
      {outboxQuery.data ? (
        <>
          <p className={styles.meta} aria-live="polite">
            Pending: <strong>{outboxQuery.data.lag.pending}</strong> ·
            Retrying: <strong>{outboxQuery.data.lag.retrying}</strong> ·
            Dead-lettered:{" "}
            <strong>{outboxQuery.data.lag.dead_lettered}</strong> · Total:{" "}
            <strong>{outboxQuery.data.lag.total}</strong>
          </p>
          {outboxQuery.data.items.length === 0 ? (
            <p className={styles.empty}>
              No pending rows. The outbox is healthy.
            </p>
          ) : (
            <ul className={styles.list}>
              {outboxQuery.data.items.map((row) => (
                <li key={row.id} className={styles.listItem}>
                  <code>{row.event_type}</code>{" "}
                  <span className={styles.meta}>
                    attempts {row.attempt_count}
                    {row.is_dead_lettered ? " · dead-lettered" : ""}
                  </span>
                  <p className={styles.meta}>
                    Last error: {row.last_error ?? "—"}
                  </p>
                  {confirmRowId === row.id ? (
                    <p>
                      <span className={styles.meta}>
                        Confirm replay of {row.event_type}?{" "}
                      </span>
                      <button
                        type="button"
                        disabled={replayRow.isPending}
                        onClick={() => replayRow.mutate(row.id)}
                      >
                        {replayRow.isPending
                          ? "Replaying…"
                          : "Confirm replay"}
                      </button>{" "}
                      <button
                        type="button"
                        disabled={replayRow.isPending}
                        onClick={() => setConfirmRowId(null)}
                      >
                        Cancel
                      </button>
                    </p>
                  ) : (
                    <button
                      type="button"
                      aria-label={`Replay ${row.event_type} row ${row.id}`}
                      onClick={() => {
                        setActionError(null);
                        setNotice(null);
                        setConfirmRowId(row.id);
                      }}
                    >
                      Replay
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}

      <h3 id="ops-event-catalog">Event catalog compatibility</h3>
      {builtinCatalogQuery.isLoading || extensionCatalogQuery.isLoading ? (
        <p>Loading event catalog…</p>
      ) : null}
      {builtinCatalogQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(builtinCatalogQuery.error)}
        </p>
      ) : null}
      {extensionCatalogQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(extensionCatalogQuery.error)}
        </p>
      ) : null}
      {builtinCatalogQuery.data && extensionCatalogQuery.data ? (
        <>
          {breaking.length > 0 ? (
            <p role="alert" className={styles.error}>
              Breaking change: {breaking.length} extension{" "}
              {breaking.length === 1 ? "entry" : "entries"} narrows a
              built-in version&apos;s payload keys (
              {breaking
                .map(
                  (entry) =>
                    `${entry.event_type} v${entry.version}: removed ${entry.removed_keys.join(", ")}`,
                )
                .join("; ")}
              ). Ship a new version instead.
            </p>
          ) : (
            <p className={styles.meta}>
              Compatible: {catalog.length} event{" "}
              {catalog.length === 1 ? "type" : "types"} across built-in and
              extension registries, no payload-key removals.
            </p>
          )}
          <ul className={styles.list}>
            {catalog.map((entry) => (
              <li
                key={`${entry.source}:${entry.event_type}@${entry.version}`}
                className={styles.listItem}
              >
                <code>{entry.event_type}</code>{" "}
                <span className={styles.meta}>
                  v{entry.version} · {entry.source}
                </span>
                <p className={styles.meta}>
                  {entry.description} — keys:{" "}
                  {entry.payload_keys.join(", ") || "—"}
                </p>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <h3 id="ops-webhooks">Webhook subscriptions</h3>
      {subscriptionsQuery.isLoading ? <p>Loading subscriptions…</p> : null}
      {subscriptionsQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(subscriptionsQuery.error)}
        </p>
      ) : null}
      {subscriptionsQuery.data ? (
        subscriptionsQuery.data.items.length === 0 ? (
          <p className={styles.empty}>
            No outbound webhook subscriptions in this tenant.
          </p>
        ) : (
          <ul className={styles.list}>
            {subscriptionsQuery.data.items.map((subscription) => (
              <li key={subscription.id} className={styles.listItem}>
                <code>{subscription.url}</code>{" "}
                <span className={styles.meta}>
                  status {subscription.status} ·{" "}
                  {subscription.signing_algorithm}
                </span>
                <p className={styles.meta}>
                  {subscription.description || "No description"} — filters:{" "}
                  {subscription.event_filters.join(", ") || "—"}
                </p>
                {confirmSubscriptionId === subscription.id ? (
                  <p>
                    <span className={styles.meta}>
                      Confirm replay of dead-lettered deliveries?{" "}
                    </span>
                    <button
                      type="button"
                      disabled={replaySubscription.isPending}
                      onClick={() =>
                        replaySubscription.mutate(subscription.id)
                      }
                    >
                      {replaySubscription.isPending
                        ? "Replaying…"
                        : "Confirm replay"}
                    </button>{" "}
                    <button
                      type="button"
                      disabled={replaySubscription.isPending}
                      onClick={() => setConfirmSubscriptionId(null)}
                    >
                      Cancel
                    </button>
                  </p>
                ) : (
                  <button
                    type="button"
                    aria-label={`Replay dead-lettered deliveries for ${subscription.description || subscription.url}`}
                    onClick={() => {
                      setActionError(null);
                      setNotice(null);
                      setConfirmSubscriptionId(subscription.id);
                    }}
                  >
                    Replay dead-lettered
                  </button>
                )}
              </li>
            ))}
          </ul>
        )
      ) : null}

      <h3 id="ops-diagnostics">Diagnostics &amp; config impact</h3>
      {diagnosticsQuery.isLoading || storageQuery.isLoading ? (
        <p>Loading diagnostics…</p>
      ) : null}
      {diagnosticsQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(diagnosticsQuery.error)}
        </p>
      ) : null}
      {storageQuery.error ? (
        <p role="alert" className={styles.error}>
          {errorMessage(storageQuery.error)}
        </p>
      ) : null}
      {diagnosticsQuery.data ? (
        <>
          <p className={styles.meta}>
            Synthetic environment:{" "}
            <strong>
              {diagnosticsQuery.data.synthetic_environment ? "yes" : "no"}
            </strong>
          </p>
          <ul className={styles.list}>
            {Object.entries(diagnosticsQuery.data.services_actual).map(
              ([service, actual]) => {
                const desired =
                  diagnosticsQuery.data.services_desired[service];
                const mismatch =
                  desired !== undefined &&
                  formatConfigValue(desired) !== formatConfigValue(actual);
                return (
                  <li key={service} className={styles.listItem}>
                    <code>{service}</code>{" "}
                    <span className={styles.meta}>
                      desired {formatConfigValue(desired)} · actual{" "}
                      {formatConfigValue(actual)}
                    </span>
                    {mismatch ? (
                      <p className={styles.meta}>
                        Desired and actual differ — a config change may be
                        pending rollout or a service may be down.
                      </p>
                    ) : null}
                  </li>
                );
              },
            )}
          </ul>
        </>
      ) : null}
      {storageQuery.data ? (
        <p className={styles.meta}>
          Storage volume: desired{" "}
          {formatConfigValue(
            (storageQuery.data.declared_volume as Record<string, unknown>)
              .adapter ??
              (storageQuery.data.declared_volume as Record<string, unknown>)
                .tier,
          )}{" "}
          · actual{" "}
          {formatConfigValue(
            (storageQuery.data.actual_volume as Record<string, unknown>)
              .adapter,
          )}
          ; encryption: desired{" "}
          {formatConfigValue(
            (storageQuery.data.declared_encryption as Record<string, unknown>)
              .enabled,
          )}{" "}
          · actual{" "}
          {formatConfigValue(
            (storageQuery.data.actual_encryption as Record<string, unknown>)
              .enabled,
          )}
          . Key material is never shown here.
        </p>
      ) : null}
    </section>
  );
}
