import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  ApiClientError,
  CASES_PAGE_LIMIT_DEFAULT,
  CASES_PAGE_LIMIT_MAX,
  api,
} from "./client";
import type { CaseListItem, CaseSummary, Page } from "./types";

function mockFetchOnce(body: unknown, init?: { status?: number }): void {
  const status = init?.status ?? 200;
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

describe("api client (P01-US2 / FR-002)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listCases issues GET /api/v1/cases and unwraps the page envelope", async () => {
    const page: Page<CaseListItem> = {
      items: [
        {
          id: "11111111-1111-1111-1111-111111111111",
          tenant_id: "22222222-2222-2222-2222-222222222222",
          reference: "C-ACME-2026-000001",
          case_type_key: "ServiceRequest",
          status: "open",
          priority: "normal",
          title: "Help with onboarding",
          current_workflow_state: "intake",
          version_token: 1,
          created_at: "2026-08-31T12:00:00+00:00",
          updated_at: "2026-08-31T12:00:00+00:00",
          created_by: "33333333-3333-3333-3333-333333333333",
          closed_at: null,
        },
      ],
      next_cursor: null,
      limit: 50,
    };
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(page), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    const result = await api.listCases();
    expect(result.limit).toBe(50);
    expect(result.items).toHaveLength(1);
    expect(result.items[0].reference).toBe("C-ACME-2026-000001");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const called = fetchSpy.mock.calls[0];
    expect(String(called[0])).toBe("/api/v1/cases");
  });

  it("listCases forwards the limit query param verbatim", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: [], next_cursor: null, limit: 25 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    await api.listCases({ limit: 25, offset: 50 });
    const url = String(fetchSpy.mock.calls[0][0]);
    expect(url).toContain("/api/v1/cases?");
    expect(url).toContain("limit=25");
    expect(url).toContain("offset=50");
  });

  it("getCase fetches the canonical CaseSummary by id", async () => {
    const summary: CaseSummary = {
      id: "11111111-1111-1111-1111-111111111111",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      reference: "C-ACME-2026-000001",
      case_type_key: "ServiceRequest",
      case_type_version_id: "44444444-4444-4444-4444-444444444444",
      status: "open",
      priority: "normal",
      title: "Help with onboarding",
      description: "",
      data: { title: "Help with onboarding", priority: "normal" },
      current_workflow_state: "intake",
      version_token: 1,
      created_at: "2026-08-31T12:00:00+00:00",
      updated_at: "2026-08-31T12:00:00+00:00",
      created_by: "33333333-3333-3333-3333-333333333333",
      closed_at: null,
      assignee: { redacted: true, reason: "not_implemented" },
      queue: { redacted: true, reason: "not_implemented" },
      sla_state: { redacted: true, reason: "not_implemented" },
      participants: [],
      open_tasks: [],
      recent_documents: [],
      recent_decisions: [],
      timeline: [],
      legal_hold: false,
    };
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(summary), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    const result = await api.getCase(summary.id);
    expect(result.reference).toBe("C-ACME-2026-000001");
    expect(result.assignee?.redacted).toBe(true);
    expect(result.timeline).toEqual([]);
    const url = String(fetchSpy.mock.calls[0][0]);
    expect(url).toBe(`/api/v1/cases/${summary.id}`);
  });

  it("maps a 403 response to ApiClientError carrying the structured code", async () => {
    mockFetchOnce(
      {
        code: "forbidden",
        message: "case.read denied: no role grants this action",
        correlation_id: "corr-1",
      },
      { status: 403 },
    );
    let captured: unknown = null;
    try {
      await api.getCase("anything");
    } catch (err) {
      captured = err;
    }
    expect(captured).toBeInstanceOf(ApiClientError);
    const err = captured as ApiClientError;
    expect(err.status).toBe(403);
    expect(err.message).toContain("case.read denied");
    // The client surfaces the body's ``code`` as ``correlationId`` on
    // the error for backwards compatibility with earlier slices.
    expect(err.correlationId).toBe("forbidden");
  });

  it("page-limit constants match the backend contract", () => {
    expect(CASES_PAGE_LIMIT_DEFAULT).toBe(50);
    expect(CASES_PAGE_LIMIT_MAX).toBe(200);
  });

  it("emits a correlation id header on every request", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: [], next_cursor: null, limit: 50 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    await api.listCases();
    const headers = fetchSpy.mock.calls[0][1]?.headers as Record<
      string,
      string
    >;
    expect(headers["X-Correlation-Id"]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });
});

describe("ApiClientError", () => {
  it("exposes status, message, correlationId and details", () => {
    const err = new ApiClientError("boom", 422, "validation_failed", {
      field: "title",
    });
    expect(err.status).toBe(422);
    expect(err.message).toBe("boom");
    expect(err.correlationId).toBe("validation_failed");
    expect(err.details).toEqual({ field: "title" });
    expect(err).toBeInstanceOf(Error);
  });
});

describe("client basics", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("createCase posts JSON to /api/v1/cases", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "1",
            tenant_id: "2",
            reference: "C-ACME-2026-000001",
            case_type_key: "ServiceRequest",
            case_type_version_id: "3",
            status: "open",
            priority: "normal",
            title: "x",
            description: "",
            data: {},
            current_workflow_state: null,
            version_token: 1,
            created_at: "2026-08-31T12:00:00+00:00",
            updated_at: "2026-08-31T12:00:00+00:00",
            created_by: "9",
            closed_at: null,
            assignee: null,
            queue: null,
            sla_state: null,
            participants: [],
            open_tasks: [],
            recent_documents: [],
            recent_decisions: [],
            timeline: [],
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        ),
      );
    await api.createCase({
      case_type_key: "ServiceRequest",
      title: "x",
      description: "",
      data: {},
    });
    const init = fetchSpy.mock.calls[0][1];
    expect(init?.method).toBe("POST");
    expect(String(fetchSpy.mock.calls[0][0])).toBe("/api/v1/cases");
  });
});