/**
 * Configuration publication history and second-admin approval (FM-021).
 */
import type { components } from "../types.gen";
import type { PublicationHistory } from "./types";
import { request } from "./http";

/** Configuration publication history and second-admin approval (FM-021). */
export const publications = {
  /** List publication history, newest first, optionally scoped. */
  list: (params: { artifact_type?: string; artifact_id?: string } = {}) => {
    const search = new URLSearchParams();
    if (params.artifact_type !== undefined)
      search.set("artifact_type", params.artifact_type);
    if (params.artifact_id !== undefined)
      search.set("artifact_id", params.artifact_id);
    const qs = search.toString();
    return request<PublicationHistory>(
      `/admin/publications${qs ? `?${qs}` : ""}`,
    );
  },
  /** Append a publication row; body reports whether approval is required. */
  record: (body: components["schemas"]["RecordPublicationRequest"]) =>
    request<components["schemas"]["RecordPublicationResponse"]>(
      "/admin/publications",
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
  /** Record a second administrator's approval (separation of duties). */
  approve: (body: components["schemas"]["ApprovePublicationRequest"]) =>
    request<components["schemas"]["ApprovePublicationResponse"]>(
      "/admin/publications/approve",
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
};
