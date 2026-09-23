/**
 * Shared document-viewer query keys (C-8 / T5 viewer pipeline).
 *
 * Centralized so invalidation in one place — e.g. after an annotation
 * create or a checkout — propagates to the viewer, the revision history,
 * and the checkout indicator. The shape mirrors ``caseQueryKeys``:
 * `["documents", id, ...]` for per-document surfaces.
 */
export const documentQueryKeys = {
  all: ["documents"] as const,
  detail: (documentId: string) => ["documents", documentId] as const,
  annotations: (documentId: string, version?: number, offset = 0) =>
    ["documents", documentId, "annotations", version ?? "all", offset] as const,
  revisions: (documentId: string) => ["documents", documentId, "revisions"] as const,
  versions: (documentId: string) => ["documents", documentId, "versions"] as const,
  renditions: (documentId: string) => ["documents", documentId, "renditions"] as const,
  checkoutHistory: (documentId: string) =>
    ["documents", documentId, "checkout", "history"] as const,
};

export const bundleQueryKeys = {
  all: ["bundles"] as const,
  detail: (bundleId: string) => ["bundles", bundleId] as const,
};
