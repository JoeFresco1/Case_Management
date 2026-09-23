/**
 * Records lifecycle: legal holds, retention, disposition, archival, evidence (P08 / FM-039..FM-042).
 */
import type {
  CaseArchivalResponse,
  DisposalFreezeResponse,
  DispositionRequestResponse,
  EvidenceReportResponse,
  LegalHoldResponse,
  OrphanProposalResponse,
  OrphanScanResponse,
  PlaceHoldRequest,
  PublishRetentionPolicyRequest,
  ReleaseHoldRequest,
  RetentionPolicyResponse,
} from "./types";
import { API_BASE, ApiClientError, request } from "./http";

/**
 * Records lifecycle (P08 / FM-039..FM-042): legal holds, retention
 * policies, disposition approvals, archival tiers, disposal freezes,
 * orphan proposals, and the defensible-deletion evidence report.
 *
 * Every POST carries ``Idempotency-Key`` (400 without it, 409 on key
 * reuse with a different payload). Disposition decisions are
 * concurrency-guarded: deciding an already-decided request is 409
 * ``ErrorResponse{conflict}`` so the UI refetches instead of
 * assuming a silent overwrite.
 */
export const records = {
  /** Place a legal hold on a case (records-admin only). */
  placeHold: (caseId: string, body: PlaceHoldRequest) =>
    request<LegalHoldResponse>(`/cases/${caseId}/holds`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Release the active legal hold on a case (records-admin only). */
  releaseHold: (caseId: string, body: ReleaseHoldRequest) =>
    request<LegalHoldResponse>(`/cases/${caseId}/holds`, {
      // NOTE: the route is DELETE with a JSON body; fetch supports it.
      method: "DELETE",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Publish a new retention-policy version. */
  publishRetentionPolicy: (body: PublishRetentionPolicyRequest) =>
    request<RetentionPolicyResponse>("/admin/retention-policies", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Route an eligible case into the disposition approval queue. */
  routeDisposition: (caseId: string) =>
    request<DispositionRequestResponse>(
      `/admin/disposition/eligible/${caseId}/route`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
  /** Approve a pending disposition request (409 when already decided). */
  approveDisposition: (requestId: string, reason = "") =>
    request<DispositionRequestResponse>(
      `/admin/disposition/${requestId}/approve`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ reason }),
      },
    ),
  /** Reject a pending disposition request (reason required, 422 when blank). */
  rejectDisposition: (requestId: string, reason: string) =>
    request<DispositionRequestResponse>(
      `/admin/disposition/${requestId}/reject`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ reason }),
      },
    ),
  /** Chain-of-custody evidence report for a case (policy + source time). */
  getEvidenceReport: (caseId: string) =>
    request<EvidenceReportResponse>(`/retention/evidence/${caseId}`),
  /** Export a portable evidence bundle (case + documents + audit ZIP). */
  exportEvidenceBundle: async (caseId: string, reason: string): Promise<Blob> => {
    const correlationId = crypto.randomUUID();
    const response = await fetch(`${API_BASE}/exports/bundle`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Correlation-Id": correlationId,
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({ case_id: caseId, reason }),
    });
    if (!response.ok) {
      let body: { message?: string } = {};
      try {
        body = await response.json();
      } catch {
        // ignore parse errors (ZIP error paths are JSON)
      }
      throw new ApiClientError(
        body.message ?? `request failed: ${response.status}`,
        response.status,
      );
    }
    return await response.blob();
  },
};

// Archival / freeze / orphan helpers share the records base paths; expose
// the response aliases so consumers need not import from ./types directly.
export type {
  CaseArchivalResponse,
  DisposalFreezeResponse,
  OrphanProposalResponse,
  OrphanScanResponse,
};
