import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CreateCasePage } from "./CreateCasePage";

const published = {
  id: "11111111-1111-1111-1111-111111111111",
  key: "ServiceRequest",
  name: "Service request",
  description: "Synthetic requests",
  sensitivity_default: "internal",
  version_id: "22222222-2222-2222-2222-222222222222",
  version_number: 1,
  schema: { fields: [], document_classifications: [], rules: [] },
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CreateCasePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CreateCasePage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("retrieves published types and renders the schema-driven form", async () => {
    vi.spyOn(api, "listCaseTypes").mockResolvedValue({ items: [published], next_cursor: null, limit: 50 });
    renderPage();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Create case" })).toBeInTheDocument());
    expect(screen.getByLabelText("Case type")).toHaveValue("ServiceRequest");
    expect(screen.getByRole("button", { name: "Create case" })).toBeInTheDocument();
  });

  it("shows an honest empty state when no published type is available", async () => {
    vi.spyOn(api, "listCaseTypes").mockResolvedValue({ items: [], next_cursor: null, limit: 50 });
    renderPage();
    await waitFor(() => expect(screen.getByText(/No published case types/)).toBeInTheDocument());
  });
});
