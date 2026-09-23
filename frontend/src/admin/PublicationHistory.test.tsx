import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublicationHistory } from "./PublicationHistory";

function renderHistory() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PublicationHistory />
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200 });
}

const historyFixture = {
  items: [
    {
      id: "pub-1",
      tenant_id: "tenant-1",
      artifact_type: "CaseTypeVersion",
      artifact_id: "art-1",
      version_id: "ver-1",
      action: "publish",
      actor_id: "actor-1111",
      occurred_at: "2026-09-01T00:00:00Z",
      change_summary: "Initial ServiceRequest schema",
    },
  ],
};

describe("PublicationHistory", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the governance timeline newest first", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse(historyFixture));
    renderHistory();
    await waitFor(() => expect(screen.getByText("CaseTypeVersion")).toBeInTheDocument());
    // "publish" appears both as the timeline action badge and the form
    // option — the badge is what proves the record rendered.
    expect(screen.getAllByText("publish").length).toBeGreaterThan(0);
    expect(screen.getByText("Initial ServiceRequest schema")).toBeInTheDocument();
  });

  it("surfaces approval-required state after recording", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          publication_id: "pub-2",
          action: "publish",
          approval_required: true,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [] }));
    renderHistory();
    await waitFor(() => expect(screen.getByText("Record a publication")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Artifact type"), {
      target: { value: "CaseTypeVersion" },
    });
    fireEvent.change(screen.getByLabelText("Artifact id"), {
      target: { value: "art-1" },
    });
    fireEvent.change(screen.getByLabelText("Version id"), {
      target: { value: "ver-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record publication" }));
    // list, POST record, then the success-invalidation list refetch
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(3));
    const [url, init] = fetchSpy.mock.calls[1] as [string, RequestInit];
    expect(url).toContain("/api/v1/admin/publications");
    expect(init.method).toBe("POST");
    expect(
      await screen.findByText(/Second-admin approval required/),
    ).toBeInTheDocument();
  });
});
