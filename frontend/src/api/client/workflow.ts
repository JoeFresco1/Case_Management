/**
 * Workflow transition endpoints (P02 / FM-010..FM-013).
 */
import type { ExecuteTransitionResponse, TransitionPreflightResponse } from "./types";
import { request } from "./http";

/** List authorized workflow transitions with deterministic guard verdicts. */
export function listTransitions(caseId: string): Promise<TransitionPreflightResponse> {
  return request<TransitionPreflightResponse>(`/cases/${caseId}/transitions`);
}

/** Execute a case workflow transition with server-side guard evaluation. */
export function executeTransition(
  caseId: string,
  transitionKey: string,
): Promise<ExecuteTransitionResponse> {
  return request<ExecuteTransitionResponse>(`/cases/${caseId}/transitions`, {
    method: "POST",
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify({ transition_key: transitionKey, payload: null }),
  });
}
