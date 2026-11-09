import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "./Dashboard";

function renderWithClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Dashboard refreshIntervalMs={60_000} />
    </QueryClientProvider>,
  );
}

describe("Dashboard (FM-054)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads the typed operational snapshot without dev identity headers", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kind: "snapshot",
          generated_at: "2026-09-05T12:00:00Z",
          served_at: "2026-09-05T12:00:01Z",
          freshness_seconds: 1,
          tenant_id: "22222222-2222-2222-2222-222222222222",
          actor_id: "33333333-3333-3333-3333-333333333333",
          team: ["intake_queue"],
          kpis: {
            total_open_cases: 4,
            total_overdue_tasks: 2,
            open_by_priority: { urgent: 1, normal: 3 },
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    renderWithClient();
    await waitFor(() => expect(screen.getByText("4")).toBeInTheDocument());
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText(/intake_queue/)).toBeInTheDocument();
    expect(screen.getByText("25%")).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByLabelText("1 case")).toHaveTextContent("1");
    expect(screen.getByLabelText("3 cases")).toHaveTextContent("3");
    expect(screen.getByRole("link", { name: /view tasks/i })).toHaveAttribute(
      "href",
      "/tasks",
    );
    const [, init] = fetchSpy.mock.calls[0];
    expect(
      (init?.headers as Record<string, string>)?.["X-Dev-User"],
    ).toBeUndefined();
    expect(
      (init?.headers as Record<string, string>)?.["X-Dev-Tenant"],
    ).toBeUndefined();
  });
  it("shows access restrictions without rendering zero metrics", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("{}", { status: 403 }),
    );
    renderWithClient();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Dashboard access is restricted",
    );
    expect(screen.queryByText("Open cases", { exact: true })).toBeNull();
  });
});
