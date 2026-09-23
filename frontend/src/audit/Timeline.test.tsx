import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { Timeline } from "./Timeline";

describe("Timeline", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders generated timeline entries returned for the case", async () => {
    vi.spyOn(api, "getCaseTimeline").mockResolvedValue({
      items: [{
        id: "event-1",
        event_type: "CaseCreated",
        occurred_at: "2026-09-06T12:00:00Z",
        actor_id: "actor-1",
        actor_type: "user",
        subject_type: "case",
        subject_id: "case-1",
        correlation_id: "corr-1",
        causation_id: null,
        payload: { reference: "C-ACME-2026-000001" },
        hash: "hash-1",
        prev_hash: "",
        config_version: null,
        redacted: false,
      }],
      next_cursor: null,
      limit: 50,
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <Timeline caseId="case-1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("CaseCreated")).toBeInTheDocument();
    expect(screen.getByText(/actor-1/)).toBeInTheDocument();
    await waitFor(() => expect(api.getCaseTimeline).toHaveBeenCalledWith("case-1", expect.objectContaining({ limit: 50 })));
  });
});
