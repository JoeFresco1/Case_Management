/**
 * Shared wire-shape type aliases for the API client.
 *
 * Every alias points at a generated OpenAPI schema (or the local
 * hand-written contracts in `../types`); field lists are never
 * re-declared here so drift surfaces at typecheck time.
 */
import type { components } from "../types.gen";

/** Published form version wire shape (E-6 / FM-036). Generated, never hand-typed. */
export type FormVersionResponse =
  components["schemas"]["FormVersionResponse"];
/** Declarative form body shared by runtime + visual designer (single format). */
export type FormDefinitionWire = components["schemas"]["FormDefinition"];
/** Single-step submit wire shapes (E-6 submission). */
export type SubmitFormRequestWire = components["schemas"]["SubmitFormRequest"];
export type SubmitFormResponseWire =
  components["schemas"]["SubmitFormResponse"];
/** Backward-compatible alias: single-step submit response. */
export type SubmitFormResponse = SubmitFormResponseWire;
/** Multi-step session wire shapes (E-6 resume, P07-US6 / FM-038). */
export type FormSessionResponse =
  components["schemas"]["FormSessionResponse"];
export type FormStepSpec = components["schemas"]["FormStepSpec"];
export type StartFormSessionRequest =
  components["schemas"]["StartFormSessionRequest"];
export type SaveFormStepRequest =
  components["schemas"]["SaveFormStepRequest"];

export type TaskView = components["schemas"]["TaskView"];
export type TaskPage = components["schemas"]["Page_TaskView_"];
export type TimelinePage = components["schemas"]["Page_TimelineEntryResponse_"];
export type PublishedCaseType = components["schemas"]["PublishedCaseTypeResponse"];
export type PublishedCaseTypeList = components["schemas"]["PublishedCaseTypeListResponse"];

export type RoleSummary = components["schemas"]["RoleSummary"];
export type DashboardResponse = components["schemas"]["DashboardResponse"];
export type StoredPreferencesResponse =
  components["schemas"]["StoredPreferencesResponse"];
export type EffectivePreferencesResponse =
  components["schemas"]["EffectivePreferencesResponse"];
export type PreferencePolicyResponse =
  components["schemas"]["PreferencePolicyResponse"];
export type SaveDashboardRequest =
  components["schemas"]["SaveDashboardRequest"];
export type SavePreferencesRequest =
  components["schemas"]["SavePreferencesRequest"];
export type PublicationRecord =
  components["schemas"]["PublicationRecordResponse"];
export type PublicationHistory =
  components["schemas"]["app__interfaces__http__schemas__admin_governance__PublicationHistoryResponse"];
export type OperationalDashboard =
  components["schemas"]["OperationalDashboardResponse"];
export type ExecuteTransitionResponse =
  components["schemas"]["ExecuteTransitionResponse"];
export type TransitionPreflightResponse =
  components["schemas"]["TransitionPreflightResponse"];

/**
 * Import pipeline contracts (P14-US2 / FM-026, D-6).
 *
 * Every shape is a generated OpenAPI schema — the client aliases them
 * but never re-declares field lists, so drift surfaces at typecheck
 * time instead of at runtime.
 */
export type ImportJob = components["schemas"]["ImportJobResponse"];
export type ImportRowsPayload = components["schemas"]["ImportRowsPayload"];
export type MappingVersionRequest =
  components["schemas"]["MappingVersionRequest"];
export type PreviewResponse = components["schemas"]["PreviewResponse"];
export type CommitResponse = components["schemas"]["CommitResponse"];
export type ImportJobPage =
  components["schemas"]["Page_ImportJobListItemResponse_"];
export type InspectQuarantineResponse =
  components["schemas"]["InspectQuarantineResponse"];
export type QuarantineRecordResponse =
  components["schemas"]["QuarantineRecordResponse"];
export type ResolveQuarantineResponse =
  components["schemas"]["ResolveQuarantineResponse"];

/**
 * Records, evidence & reporting contracts (T6 / FM-039..FM-042, FM-051, FM-053, FM-054, FM-060).
 *
 * Every shape is a generated OpenAPI schema — the client aliases them
 * but never re-declares field lists, so drift surfaces at typecheck
 * time instead of at runtime. Mutations carry ``Idempotency-Key``;
 * reads expose ``generated_at`` source-time + ``team`` scope so the UI
 * can render policy/source-time affordances without hand-typing.
 */
export type LegalHoldResponse = components["schemas"]["LegalHoldResponse"];
export type PlaceHoldRequest = components["schemas"]["PlaceHoldRequest"];
export type ReleaseHoldRequest = components["schemas"]["ReleaseHoldRequest"];
export type RetentionPolicyResponse = components["schemas"]["RetentionPolicyResponse"];
export type PublishRetentionPolicyRequest =
  components["schemas"]["PublishRetentionPolicyRequest"];
export type DispositionRequestResponse =
  components["schemas"]["DispositionRequestResponse"];
export type CaseArchivalResponse = components["schemas"]["CaseArchivalResponse"];
export type DisposalFreezeResponse = components["schemas"]["DisposalFreezeResponse"];
export type OrphanProposalResponse = components["schemas"]["OrphanProposalResponse"];
export type OrphanScanResponse = components["schemas"]["OrphanScanResponse"];
export type EvidenceReportResponse = components["schemas"]["EvidenceReportResponse"];
export type PolicyEvidenceEntry = components["schemas"]["PolicyEvidenceEntry"];
export type OpenCasesReportResponse =
  components["schemas"]["OpenCasesReportResponse"];
export type OverdueTasksReportResponse =
  components["schemas"]["OverdueTasksReportResponse"];
export type CustomReportDefinitionResponse =
  components["schemas"]["CustomReportDefinitionResponse"];
export type CustomReportResultResponse =
  components["schemas"]["CustomReportResultResponse"];
export type ReportExportResponse = components["schemas"]["ReportExportResponse"];

/** Viewer pipeline + bundle sharing contracts (T5 / FM-023 ext, T5-US3). */
export type AnnotationResponse = components["schemas"]["AnnotationResponse"];
export type AnnotationCreateRequest =
  components["schemas"]["AnnotationCreateRequest"];
export type AnnotationUpdateRequest =
  components["schemas"]["AnnotationUpdateRequest"];
export type CheckoutResponse = components["schemas"]["CheckoutResponse"];
export type RenditionResponse = components["schemas"]["RenditionResponse"];
export type RevisionHistoryResponse =
  components["schemas"]["RevisionHistoryResponse"];
export type RevisionEntryResponse =
  components["schemas"]["RevisionEntryResponse"];
export type ReindexResponse = components["schemas"]["ReindexResponse"];
export type DocumentVersionResponse =
  components["schemas"]["DocumentVersionResponse"];
export type BundleResponse = components["schemas"]["BundleResponse"];
export type MintLinkResponse = components["schemas"]["MintLinkResponse"];
export type RedeemLinkResponse = components["schemas"]["RedeemLinkResponse"];

/** Generated request contract for POST /api/v1/cases; priority has a server default. */
export type CreateCaseRequest = Omit<components["schemas"]["CreateCaseRequest"], "priority"> & {
  priority?: components["schemas"]["CreateCaseRequest"]["priority"];
};
