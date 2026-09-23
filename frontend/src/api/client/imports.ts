/**
 * Import pipeline and quarantine operator console (P14-US2 / FM-026, FM-027).
 */
import type {
  CommitResponse,
  ImportJob,
  ImportJobPage,
  ImportRowsPayload,
  InspectQuarantineResponse,
  PreviewResponse,
  QuarantineRecordResponse,
  ResolveQuarantineResponse,
} from "./types";
import { request } from "./http";

/**
 * Import pipeline (P14-US2 / FM-026, D-6).
 *
 * Dry-run validates without writing case data; commit re-validates
 * and creates one case per accepted row. Both mutations require
 * ``Idempotency-Key``; replays return the stored response verbatim.
 * Read endpoints let the operator reconcile a past preview/commit.
 */
export const imports = {
  /** Validate an import package without writing any case data. */
  dryRun: (body: ImportRowsPayload) =>
    request<PreviewResponse>("/imports/dry-run", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Commit an import package — create cases for accepted rows. */
  commit: (body: ImportRowsPayload) =>
    request<CommitResponse>("/imports/commit", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Fetch one import job with its per-row reconciliation. */
  get: (jobId: string) =>
    request<ImportJob>(`/imports/${encodeURIComponent(jobId)}`),
  /** List import jobs, newest first (limit default 50, max 200). */
  list: (params: { limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined)
      search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<ImportJobPage>(`/imports${qs ? `?${qs}` : ""}`);
  },
};

/**
 * Quarantine operator console (P04-US5 / FM-027, D-6).
 *
 * Held records failed channel validation. Release re-processes the
 * (possibly corrected) row; reject drops it terminally. Both
 * mutations are audited server-side and require ``Idempotency-Key``.
 */
export const quarantine = {
  /** List quarantine records (default held, limit 50 max 200). */
  list: (
    params: { status?: string; limit?: number; offset?: number } = {},
  ) => {
    const search = new URLSearchParams();
    search.set("status", params.status ?? "held");
    search.set("limit", String(params.limit ?? 50));
    if (params.offset !== undefined)
      search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<InspectQuarantineResponse>(
      `/admin/quarantine${qs ? `?${qs}` : ""}`,
    );
  },
  /** Release a held record back into the pipeline. */
  release: (recordId: string, note = "") =>
    request<ResolveQuarantineResponse>(
      `/admin/quarantine/${encodeURIComponent(recordId)}/release`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ note }),
      },
    ),
  /** Reject a held record (terminal). */
  reject: (recordId: string, note = "") =>
    request<ResolveQuarantineResponse>(
      `/admin/quarantine/${encodeURIComponent(recordId)}/reject`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ note }),
      },
    ),
};

// Re-export the record shape so consumers can type quarantine rows without
// reaching into the types module directly.
export type { QuarantineRecordResponse };
