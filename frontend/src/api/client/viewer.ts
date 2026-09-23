/**
 * Document viewer pipeline (T5 / FM-023 ext): annotations, revisions,
 * renditions, checkout/checkin, re-index over ``/documents``.
 */
import type {
  AnnotationCreateRequest,
  AnnotationResponse,
  AnnotationUpdateRequest,
  CheckoutResponse,
  DocumentVersionResponse,
  ReindexResponse,
  RevisionHistoryResponse,
  RenditionResponse,
} from "./types";
import { API_BASE, request, requestVoid } from "./http";

/**
 * Viewer pipeline (T5 / FM-023 ext): annotations, revisions, renditions,
 * checkout/checkin, re-index over ``/documents``.
 *
 * Annotations are an overlay layer — they never mutate document bytes.
 * Every mutation carries ``Idempotency-Key``; checkout conflicts surface
 * as 409 with holder + expiry in ``details``.
 */
export const viewer = {
  /** List annotation overlays for a document, oldest first. */
  listAnnotations: (
    documentId: string,
    params: { document_version?: number; limit?: number; offset?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.document_version !== undefined)
      search.set("document_version", String(params.document_version));
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<AnnotationResponse[]>(
      `/documents/${documentId}/annotations${qs ? `?${qs}` : ""}`,
    );
  },
  /** Create one annotation overlay mark (never touches document bytes). */
  createAnnotation: (documentId: string, body: AnnotationCreateRequest) =>
    request<AnnotationResponse>(`/documents/${documentId}/annotations`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Edit an annotation overlay (geometry/caption only). */
  updateAnnotation: (
    documentId: string,
    annotationId: string,
    body: AnnotationUpdateRequest,
  ) =>
    request<AnnotationResponse>(
      `/documents/${documentId}/annotations/${annotationId}`,
      {
        method: "PATCH",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      },
    ),
  /** Delete an annotation overlay (audited, 204). */
  deleteAnnotation: (documentId: string, annotationId: string) =>
    requestVoid(`/documents/${documentId}/annotations/${annotationId}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  /** Unified revision + rendition history (filed versions + burned renditions). */
  getRevisions: (documentId: string) =>
    request<RevisionHistoryResponse>(`/documents/${documentId}/revisions`),
  /** List immutable version rows for a document. */
  listVersions: (documentId: string) =>
    request<DocumentVersionResponse[]>(`/documents/${documentId}/versions`),
  /** List burned rendition rows, oldest first. */
  listRenditions: (
    documentId: string,
    params: { limit?: number; offset?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<RenditionResponse[]>(
      `/documents/${documentId}/renditions${qs ? `?${qs}` : ""}`,
    );
  },
  /** Acquire (or renew, when holder) the exclusive checkout lock. */
  checkout: (documentId: string, leaseSeconds?: number) =>
    request<CheckoutResponse>(`/documents/${documentId}/checkout`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(
        leaseSeconds === undefined ? {} : { lease_seconds: leaseSeconds },
      ),
    }),
  /** Release the holder's checkout (row stays for history). */
  checkin: (documentId: string) =>
    request<CheckoutResponse>(`/documents/${documentId}/checkin`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  /** Checkout trail, newest first (who held the document when). */
  checkoutHistory: (
    documentId: string,
    params: { limit?: number; offset?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<CheckoutResponse[]>(
      `/documents/${documentId}/checkout/history${qs ? `?${qs}` : ""}`,
    );
  },
  /** Request a search-index rebuild (202: rebuild runs async). */
  requestReindex: (documentId: string, reason: string) =>
    request<ReindexResponse>(`/documents/${documentId}/reindex`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ reason }),
    }),
  /** Download URL for authorized bytes (caller fetches as blob). */
  contentUrl: (documentId: string, rendition: "original" | "redacted" = "original") =>
    `${API_BASE}/documents/${documentId}/content?rendition=${rendition}`,
};
