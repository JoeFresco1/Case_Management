import { afterEach, describe, expect, it, vi } from "vitest";
import { simulateWorkflow } from "./api";

describe("workflow simulation API client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("uses shared correlation handling without a mutation key", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ outcome: "completed" }), { status: 200 }),
      );

    await simulateWorkflow("workflow-id", 2, { case: { priority: "normal" } });

    expect(String(fetchSpy.mock.calls[0][0])).toBe(
      "/api/v1/admin/workflows/workflow-id/versions/2/simulate",
    );
    const headers = new Headers(fetchSpy.mock.calls[0][1]?.headers);
    expect(headers.get("X-Correlation-Id")).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    expect(headers.has("Idempotency-Key")).toBe(false);
  });
});
