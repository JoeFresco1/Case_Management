/**
 * Case search, saved searches, and search audit (P06 / FM-031..FM-033).
 */
import type { CaseSearchResponse, SavedSearch, SavedSearchList, SearchAuditList, SearchFilterRequest, SortEntry } from "../types";
import { request } from "./http";

/**
 * Structured case search (P06 / FM-031). The POST variant accepts a
 * controlled filter list; the GET variant accepts query-string
 * shortcuts for browser navigation.
 */
export function searchCases(body: {
  query?: string;
  filters?: SearchFilterRequest[];
  sort?: SortEntry[];
  limit?: number;
  offset?: number;
}): Promise<CaseSearchResponse> {
  return request<CaseSearchResponse>("/search", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function searchCasesGet(params: {
  query?: string;
  status?: string;
  priority?: string;
  dataKey?: string;
  dataValue?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<CaseSearchResponse> {
  const search = new URLSearchParams();
  if (params.query !== undefined) {
    search.set("query", params.query);
  }
  if (params.status !== undefined) {
    search.set("status", params.status);
  }
  if (params.priority !== undefined) {
    search.set("priority", params.priority);
  }
  if (params.dataKey !== undefined) {
    search.set("data", params.dataKey);
  }
  if (params.dataValue !== undefined) {
    search.set("field_value", params.dataValue);
  }
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<CaseSearchResponse>(`/cases/search${qs ? `?${qs}` : ""}`);
}

/**
 * Saved-search lifecycle (P06-US3, US5 / FM-032).
 *
 * The endpoints mirror the case-search POST endpoints; the run
 * endpoint returns the same ``CaseSearchResponse`` the live search
 * returns so the worker can drop the saved result into the same
 * UI affordance.
 */
export function listSavedSearches(params: { limit?: number; offset?: number } = {}): Promise<SavedSearchList> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<SavedSearchList>(`/search/saved${qs ? `?${qs}` : ""}`);
}

export function saveSearch(body: {
  name: string;
  query?: string;
  filters?: SearchFilterRequest[];
  sort?: SortEntry[];
}): Promise<SavedSearch> {
  return request<SavedSearch>("/search/saved", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteSavedSearch(id: string): Promise<null> {
  return request<null>(`/search/saved/${id}`, { method: "DELETE" });
}

export function runSavedSearch(
  id: string,
  params: { limit?: number; offset?: number } = {},
): Promise<CaseSearchResponse> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<CaseSearchResponse>(
    `/search/saved/${id}/run${qs ? `?${qs}` : ""}`,
    { method: "POST" },
  );
}

/**
 * Security-staff search audit (P06-US5 / FM-032). The endpoint
 * lives at ``/api/v1/search_audit`` (singular prefix) to mirror the
 * audit router's naming convention.
 */
export function listSearchAudit(params: {
  actor_id?: string;
  saved_search_id?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<SearchAuditList> {
  const search = new URLSearchParams();
  if (params.actor_id !== undefined) {
    search.set("actor_id", params.actor_id);
  }
  if (params.saved_search_id !== undefined) {
    search.set("saved_search_id", params.saved_search_id);
  }
  if (params.since !== undefined) {
    search.set("since", params.since);
  }
  if (params.until !== undefined) {
    search.set("until", params.until);
  }
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<SearchAuditList>(`/search_audit${qs ? `?${qs}` : ""}`);
}
