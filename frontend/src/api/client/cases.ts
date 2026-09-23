/**
 * Case CRUD, timeline, and case-type endpoints (P01 / FM-001..FM-005).
 */
import type { CaseListItem, CaseSummary, Page } from "../types";
import type { PublishedCaseTypeList, TimelinePage, CreateCaseRequest } from "./types";
import { request } from "./http";

/** List cases for the current tenant (most recent first). */
export function listCases(params: { limit?: number; offset?: number } = {}): Promise<Page<CaseListItem>> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<Page<CaseListItem>>(`/cases${qs ? `?${qs}` : ""}`);
}

/** Fetch the canonical case-workspace summary by id. */
export function getCase(caseId: string): Promise<CaseSummary> {
  return request<CaseSummary>(`/cases/${caseId}`);
}

/** List published case-type schemas authorized for intake. */
export function listCaseTypes(limit = 50): Promise<PublishedCaseTypeList> {
  return request<PublishedCaseTypeList>(`/case-types?limit=${encodeURIComponent(String(limit))}`);
}

/** Fetch the tenant-scoped, redacted audit timeline for a case. */
export function getCaseTimeline(
  caseId: string,
  params: {
    limit?: number;
    event_type?: string[];
    actor_id?: string;
    since?: string;
    until?: string;
    cursor?: string;
  } = {},
): Promise<TimelinePage> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  for (const eventType of params.event_type ?? []) search.append("event_type", eventType);
  if (params.actor_id !== undefined) search.set("actor_id", params.actor_id);
  if (params.since !== undefined) search.set("since", params.since);
  if (params.until !== undefined) search.set("until", params.until);
  if (params.cursor !== undefined) search.set("cursor", params.cursor);
  const qs = search.toString();
  return request<TimelinePage>(`/cases/${caseId}/timeline${qs ? `?${qs}` : ""}`);
}

export function createCase(body: CreateCaseRequest): Promise<CaseSummary> {
  return request<CaseSummary>("/cases", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

/**
 * Default page size advertised by the API; mirrored from
 * `MAX_PAGE_LIMIT` / `DEFAULT_PAGE_LIMIT` in the backend
 * `view_case.py`. The server clamps any larger value, so the client
 * never sends one.
 */
export const CASES_PAGE_LIMIT_DEFAULT = 50;
export const CASES_PAGE_LIMIT_MAX = 200;
