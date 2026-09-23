import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CaseList } from "./CaseList";
import { api } from "../api/client";
import type { CaseListItem, Page } from "../api/types";

function makePage(items: CaseListItem[], limit = 50): Page<CaseListItem> {
  return { items, next_cursor: null, limit };
}

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockFetchOnce(body: unknown): void {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
}

const SAMPLE_ITEM: CaseListItem = {
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
};

describe("CaseList", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders one row per case with reference + title", async () => {
    mockFetchOnce(makePage([SAMPLE_ITEM]));
    renderWithClient(<CaseList />);
    await waitFor(() => {
      expect(screen.getByText(/C-ACME-2026-000001/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Help with onboarding/)).toBeInTheDocument();
  });

  it("renders the empty hint when the page returns zero items", async () => {
    mockFetchOnce(makePage([]));
    renderWithClient(<CaseList />);
    await waitFor(() => {
      expect(screen.getByText(/No cases yet/)).toBeInTheDocument();
    });
  });

  it("sends limit+offset zero on first page and disables both buttons on a partial page", async () => {
    // given a partial page (1 item with limit 2) — no next page exists
    const listSpy = vi
      .spyOn(api, "listCases")
      .mockResolvedValue(makePage([{ ...SAMPLE_ITEM }], 2));
    // when rendering the first page
    renderWithClient(<CaseList limit={2} />);
    // then the first request uses offset 0 and both buttons are disabled
    await waitFor(() => {
      expect(screen.getByText(/C-ACME-2026-000001/)).toBeInTheDocument();
    });
    expect(listSpy).toHaveBeenCalledWith({ limit: 2, offset: 0 });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("sends the next offset on Next and returns to offset zero on Previous", async () => {
    // given a full first page (2 items with limit 2) so Next is enabled
    const second = {
      ...SAMPLE_ITEM,
      id: "44444444-4444-4444-4444-444444444444",
      reference: "C-ACME-2026-000002",
      title: "Second case",
    };
    const listSpy = vi
      .spyOn(api, "listCases")
      .mockResolvedValueOnce(makePage([SAMPLE_ITEM, second], 2))
      .mockResolvedValueOnce(makePage([{ ...second }], 2))
      .mockResolvedValue(makePage([SAMPLE_ITEM, second], 2));
    renderWithClient(<CaseList limit={2} />);
    // when the full first page loads
    await waitFor(() => {
      expect(screen.getByText(/Second case/)).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    // when advancing to the next page
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    // then the second request carries offset == limit
    await waitFor(() => {
      expect(listSpy).toHaveBeenCalledWith({ limit: 2, offset: 2 });
    });
    await waitFor(() => {
      expect(screen.getByText("Page 2")).toBeInTheDocument();
    });
    // when navigating back
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    // then the cached first page renders again without a refetch
    // (staleTime covers the already-fetched offset zero)
    await waitFor(() => {
      expect(screen.getByText("Page 1")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });
});