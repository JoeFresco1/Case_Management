import type { components } from "./types.gen";

/** Generated case workspace contract with the server's redaction projection. */
export type CaseSummary = Omit<components["schemas"]["CaseSummary"], "sla_state" | "sensitivity"> & {
  sensitivity?: components["schemas"]["CaseSummary"]["sensitivity"];
  sla_state?:
    | components["schemas"]["CaseSummary"]["sla_state"]
    | components["schemas"]["RedactedField"]
    | null;
};

/** Generated case-list contract. Regenerate from the backend OpenAPI document. */
export type CaseListItem = components["schemas"]["CaseListItem"];

/** Generated server-side redaction marker. */
export type RedactedField = components["schemas"]["RedactedField"];

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  limit: number;
}

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  correlation_id?: string;
}

export interface CaseSearchHit {
  id: string;
  reference: string;
  priority: string;
  status: string;
  title: string;
  data_snippet: Record<string, unknown>;
}

export interface CaseSearchResponse {
  items: CaseSearchHit[];
  next_cursor: string | null;
  limit: number;
  total_considered: number;
}

export interface SearchFilterRequest {
  field: string;
  op: "eq" | "like" | "in";
  value: string | number | boolean | string[];
}

export interface SortEntry {
  field: string;
  direction: "asc" | "desc";
}

export interface SavedSearch {
  id: string;
  name: string;
  query: string | null;
  filters: SearchFilterRequest[];
  sort: SortEntry[];
  query_shape: Record<string, unknown>;
  created_at: string | null;
}

export interface SavedSearchList {
  items: SavedSearch[];
  limit: number;
}

export interface SearchAuditEntry {
  id: string;
  occurred_at: string;
  actor_id: string;
  saved_search_id: string | null;
  query_shape: Record<string, unknown>;
  result_count: number;
}

export interface SearchAuditList {
  items: SearchAuditEntry[];
  limit: number;
}
