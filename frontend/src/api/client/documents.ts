/**
 * Document upload, in-case search, and bundle sharing (P03 / FM-020, T5-US3).
 */
import type { components } from "../types.gen";
import type { BundleResponse, MintLinkResponse, RedeemLinkResponse } from "./types";
import { request } from "./http";

/** Local alias for the generated document-search payload (not part of the public export surface). */
type DocumentSearchResponse = components["schemas"]["DocumentSearchResponse"];

/**
 * In-case document search (P03-US3 / P06-US8 / FM-033). The
 * endpoint is scoped to a single case: metadata + portable LIKE
 * substring on ``file_name``, filtered by the case-type's
 * classification allowlist and the actor's sensitive-read grant.
 */
export function searchCaseDocuments(
  caseId: string,
  params: {
    query?: string;
    classification?: string;
    uploaded_by?: string;
    uploaded_since?: string;
    uploaded_until?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<DocumentSearchResponse> {
  const search = new URLSearchParams();
  if (params.query !== undefined) {
    search.set("query", params.query);
  }
  if (params.classification !== undefined) {
    search.set("classification", params.classification);
  }
  if (params.uploaded_by !== undefined) {
    search.set("uploaded_by", params.uploaded_by);
  }
  if (params.uploaded_since !== undefined) {
    search.set("uploaded_since", params.uploaded_since);
  }
  if (params.uploaded_until !== undefined) {
    search.set("uploaded_until", params.uploaded_until);
  }
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return request<DocumentSearchResponse>(
    `/cases/${caseId}/documents/search${qs ? `?${qs}` : ""}`,
  );
}

/** Upload document bytes and metadata to a case using the generated API contract. */
export function uploadDocument(
  caseId: string,
  file: File,
  metadata: { classification: string; sensitivity: string },
): Promise<components["schemas"]["DocumentResponse"]> {
  const body = new FormData();
  body.append("classification", metadata.classification);
  body.append("sensitivity", metadata.sensitivity);
  body.append("file_name", file.name);
  body.append("content", file, file.name);
  return request<components["schemas"]["DocumentResponse"]>(
    `/cases/${caseId}/documents`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body,
    },
  );
}

/**
 * Permissioned bundles + share-by-link (T5-US3).
 *
 * Bundle reads are metadata-only; share-link denials carry a bare reason
 * code (unknown/expired/revoked/exhausted) so probes learn nothing.
 */
export const bundles = {
  /** Create a bundle over tenant-scoped documents (owner = creator). */
  create: (body: components["schemas"]["CreateBundleRequest"]) =>
    request<BundleResponse>("/bundles", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Read bundle metadata (granted actors only). */
  get: (bundleId: string) => request<BundleResponse>(`/bundles/${bundleId}`),
  /** Grant a bundle role to another actor (editor only). */
  grant: (bundleId: string, body: components["schemas"]["GrantBundleAccessRequest"]) =>
    request<{ bundle_id: string; grantee_id: string }>(
      `/bundles/${bundleId}/grants`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
  /** Add/remove bundle members (editor only). */
  changeMembers: (
    bundleId: string,
    body: components["schemas"]["ChangeBundleMembersRequest"],
  ) =>
    request<BundleResponse>(`/bundles/${bundleId}/members`, {
      method: "PATCH",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Revoke a bundle (irreversible; kills grants + links). */
  revoke: (bundleId: string, reason: string) =>
    request<BundleResponse>(`/bundles/${bundleId}/revoke`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ reason }),
    }),
  /** Mint a share-by-link bearer token (raw token returned exactly once). */
  mintLink: (bundleId: string, body: components["schemas"]["MintLinkRequest"]) =>
    request<MintLinkResponse>(`/bundles/${bundleId}/links`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Redeem a bearer token (no auth; token is the credential). */
  redeemLink: (rawToken: string) =>
    request<RedeemLinkResponse>("/bundles/links/redeem", {
      method: "POST",
      body: JSON.stringify({ raw_token: rawToken }),
    }),
  /** Revoke a share link by token (bundle viewer only). */
  revokeLink: (rawToken: string, reason: string) =>
    request<{ link_id: string; bundle_id: string }>("/bundles/links/revoke", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ raw_token: rawToken, reason }),
    }),
};
