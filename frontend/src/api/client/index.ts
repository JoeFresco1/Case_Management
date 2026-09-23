/**
 * Canonical API client barrel.
 *
 * Assembles the facade object and re-exports the public surface so
 * consumers keep importing from `../api/client` unchanged.
 */
import * as cases from "./cases";
import * as workflow from "./workflow";
import * as forms from "./forms";
import * as reports from "./reports";
import * as search from "./search";
import * as documents from "./documents";
import * as tasks from "./tasks";
import * as security from "./security";
import * as admin from "./admin";
import * as home from "./home";
import * as viewerNs from "./viewer";
import * as importsNs from "./imports";
import * as retentionNs from "./retention";

/**
 * Canonical API client for the cases surface (P01-US2 / FR-002).
 *
 * The wire shapes are generated from OpenAPI; the types live in
 * `./types`. We hand-edit only the client — never the types.
 */
export const api = {
  /** List cases for the current tenant (most recent first). */
  listCases: cases.listCases,
  /** Fetch the canonical case-workspace summary by id. */
  getCase: cases.getCase,
  /** List published case-type schemas authorized for intake. */
  listCaseTypes: cases.listCaseTypes,
  /** Fetch the tenant-scoped, redacted audit timeline for a case. */
  getCaseTimeline: cases.getCaseTimeline,
  /** List authorized workflow transitions with deterministic guard verdicts. */
  listTransitions: workflow.listTransitions,
  /** Execute a case workflow transition with server-side guard evaluation. */
  executeTransition: workflow.executeTransition,
  /** Fetch the latest published form version by key (E-6 published retrieval). */
  getFormVersion: forms.getFormVersion,
  /** Submit a form against a case with idempotency and optimistic concurrency. */
  submitForm: forms.submitForm,
  /** Fetch the current authorization-scoped operational KPI snapshot. */
  getOperationalDashboard: reports.getOperationalDashboard,
  forms: forms.forms,
  createCase: cases.createCase,
  /**
   * Structured case search (P06 / FM-031). The POST variant accepts a
   * controlled filter list; the GET variant accepts query-string
   * shortcuts for browser navigation.
   */
  searchCases: search.searchCases,
  searchCasesGet: search.searchCasesGet,
  /**
   * Saved-search lifecycle (P06-US3, US5 / FM-032).
   *
   * The endpoints mirror the case-search POST endpoints; the run
   * endpoint returns the same ``CaseSearchResponse`` the live search
   * returns so the worker can drop the saved result into the same
   * UI affordance.
   */
  listSavedSearches: search.listSavedSearches,
  saveSearch: search.saveSearch,
  deleteSavedSearch: search.deleteSavedSearch,
  runSavedSearch: search.runSavedSearch,
  /**
   * Security-staff search audit (P06-US5 / FM-032). The endpoint
   * lives at ``/api/v1/search_audit`` (singular prefix) to mirror the
   * audit router's naming convention.
   */
  listSearchAudit: search.listSearchAudit,
  /**
   * In-case document search (P03-US3 / P06-US8 / FM-033). The
   * endpoint is scoped to a single case: metadata + portable LIKE
   * substring on ``file_name``, filtered by the case-type's
   * classification allowlist and the actor's sensitive-read grant.
   */
  searchCaseDocuments: documents.searchCaseDocuments,
  /** Upload document bytes and metadata to a case using the generated API contract. */
  uploadDocument: documents.uploadDocument,
  /** List tasks visible to the current actor using the generated API contract. */
  listTasks: tasks.listTasks,
  /** Complete a task idempotently and return its updated canonical view. */
  completeTask: tasks.completeTask,
  queues: tasks.queues,
  roles: security.roles,
  publications: admin.publications,
  workerHome: home.workerHome,
  viewer: viewerNs.viewer,
  bundles: documents.bundles,
  imports: importsNs.imports,
  quarantine: importsNs.quarantine,
  records: retentionNs.records,
  reports: reports.reports,
};

// Values re-exported from the original single-file client.
export { ApiClientError, isForbiddenLike } from "./http";
export { CASES_PAGE_LIMIT_DEFAULT, CASES_PAGE_LIMIT_MAX } from "./cases";

// Type re-exports (isolatedModules-safe): types.ts + local aliases.
export type {
  AnnotationResponse,
  AnnotationCreateRequest,
  AnnotationUpdateRequest,
  CaseArchivalResponse,
  CheckoutResponse,
  CommitResponse,
  CreateCaseRequest,
  CustomReportDefinitionResponse,
  CustomReportResultResponse,
  DashboardResponse,
  DisposalFreezeResponse,
  DispositionRequestResponse,
  DocumentVersionResponse,
  EffectivePreferencesResponse,
  EvidenceReportResponse,
  ExecuteTransitionResponse,
  FormDefinitionWire,
  FormSessionResponse,
  FormStepSpec,
  FormVersionResponse,
  ImportJob,
  ImportJobPage,
  ImportRowsPayload,
  InspectQuarantineResponse,
  LegalHoldResponse,
  MappingVersionRequest,
  MintLinkResponse,
  OpenCasesReportResponse,
  OperationalDashboard,
  OrphanProposalResponse,
  OrphanScanResponse,
  OverdueTasksReportResponse,
  PlaceHoldRequest,
  PolicyEvidenceEntry,
  PreferencePolicyResponse,
  PreviewResponse,
  PublicationHistory,
  PublicationRecord,
  PublishedCaseType,
  PublishedCaseTypeList,
  QuarantineRecordResponse,
  RedeemLinkResponse,
  ReleaseHoldRequest,
  ReportExportResponse,
  ReindexResponse,
  ResolveQuarantineResponse,
  RetentionPolicyResponse,
  RevisionEntryResponse,
  RevisionHistoryResponse,
  RenditionResponse,
  RoleSummary,
  SaveDashboardRequest,
  SaveFormStepRequest,
  SavePreferencesRequest,
  StartFormSessionRequest,
  StoredPreferencesResponse,
  SubmitFormRequestWire,
  SubmitFormResponse,
  SubmitFormResponseWire,
  TaskPage,
  TaskView,
  TimelinePage,
  TransitionPreflightResponse,
} from "./types";
