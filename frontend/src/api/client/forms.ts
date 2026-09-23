/**
 * Form runtime endpoints (E-6 / FM-036, FM-038).
 *
 * Single-step helpers live beside the multi-step session namespace so
 * callers can use either surface; both target the same generated wire
 * contract.
 */
import type {
  FormSessionResponse,
  FormVersionResponse,
  SaveFormStepRequest,
  StartFormSessionRequest,
  SubmitFormRequestWire,
  SubmitFormResponseWire,
} from "./types";
import { request } from "./http";

/** Fetch the latest published form version by key (E-6 published retrieval).
 *
 * Draft keys are invisible by design: the server answers 404
 * ``ErrorResponse{not_found}`` so callers cannot distinguish "no such
 * form" from "not yet published". The wire shape is generated; the
 * renderer normalizes optional sections/validations locally without
 * inventing a second format.
 */
export function getFormVersion(formKey: string): Promise<FormVersionResponse> {
  return request<FormVersionResponse>(`/forms/${encodeURIComponent(formKey)}`);
}

/** Submit a form against a case with idempotency and optimistic concurrency. */
export function submitForm(
  caseId: string,
  body: SubmitFormRequestWire,
  versionToken: number,
): Promise<SubmitFormResponseWire> {
  return request<SubmitFormResponseWire>(`/forms/cases/${caseId}`, {
    method: "POST",
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
      "If-Match": String(versionToken),
    },
    body: JSON.stringify(body),
  });
}

/**
 * Versioned form runtime over the generated E-6 contract (FM-036/FM-038).
 *
 * Single source of truth for the form wire shapes: every method aliases
 * ``components["schemas"]`` and never re-declares field lists, so drift
 * surfaces at typecheck time. The visual designer emits the same
 * ``FormDefinition`` JSON this namespace consumes — no second format.
 */
export const forms = {
  /** Fetch the latest published form version by key (drafts 404). */
  getVersion: (formKey: string) =>
    request<FormVersionResponse>(
      `/forms/${encodeURIComponent(formKey)}`,
    ),
  /** Submit a published form against a case (Idempotency-Key + If-Match). */
  submit: (
    caseId: string,
    body: SubmitFormRequestWire,
    versionToken: number,
  ) =>
    request<SubmitFormResponseWire>(`/forms/cases/${caseId}`, {
      method: "POST",
      headers: {
        "Idempotency-Key": crypto.randomUUID(),
        "If-Match": String(versionToken),
      },
      body: JSON.stringify(body),
    }),
  /** Start or resume a multi-step session (idempotent on triple). */
  startSession: (caseId: string, formKey: string) =>
    request<FormSessionResponse>(`/forms/cases/${caseId}/sessions`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ form_key: formKey } satisfies StartFormSessionRequest),
    }),
  /** Resume the open session for (case, form, actor) or reject with 404. */
  resumeSession: (caseId: string, formKey: string) =>
    request<FormSessionResponse>(
      `/forms/cases/${caseId}/sessions?form_key=${encodeURIComponent(formKey)}`,
    ),
  /** Save one step's partial data (If-Match guards concurrent saves). */
  saveStep: (
    caseId: string,
    sessionId: string,
    stepKey: string,
    body: SaveFormStepRequest,
    versionToken: number,
  ) =>
    request<FormSessionResponse>(
      `/forms/cases/${caseId}/sessions/${sessionId}/steps/${encodeURIComponent(stepKey)}`,
      {
        method: "PATCH",
        headers: { "If-Match": `"${versionToken}"` },
        body: JSON.stringify(body),
      },
    ),
};
