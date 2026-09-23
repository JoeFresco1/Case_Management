import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CreateCaseForm, validateCreateCase } from "./CreateCaseForm";
import type { components } from "../api/types.gen";

const schema: components["schemas"]["CaseTypeSchemaRequest"] = {
  fields: [
    { key: "requester", label: "Requester", type: "text", required: true, sensitivity: "internal", searchable: true },
    { key: "urgent", label: "Urgent", type: "boolean", required: false, sensitivity: "internal", searchable: false },
  ],
};

function renderForm() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><CreateCaseForm caseTypeKey="ServiceRequest" publishedSchema={schema} /></QueryClientProvider>);
}

describe("CreateCaseForm", () => {
  afterEach(() => vi.restoreAllMocks());

  it("validates title and required schema fields before submitting", async () => {
    // given a published schema with a required field
    renderForm();
    // when the user submits an empty form
    fireEvent.click(screen.getByRole("button", { name: "Create case" }));
    // then both server-aligned required-field errors are shown
    expect(screen.getByText("Title is required.")).toBeInTheDocument();
    expect(screen.getByText("Requester is required.")).toBeInTheDocument();
  });

  it("submits title, priority, and schema data", async () => {
    // given a valid form and a successful API response
    vi.spyOn(api, "createCase").mockResolvedValue({ reference: "C-ACME-2026-000001" } as never);
    renderForm();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "New request" } });
    fireEvent.change(screen.getByLabelText(/Requester/), { target: { value: "Alex" } });
    // when the user submits
    fireEvent.click(screen.getByRole("button", { name: "Create case" }));
    // then the generated-client boundary receives the schema values
    await waitFor(() => expect(api.createCase).toHaveBeenCalledWith(expect.objectContaining({
      case_type_key: "ServiceRequest",
      title: "New request",
      priority: "normal",
      data: { requester: "Alex" },
    })));
  });

  it("reports required fields using the same empty-value rule as the domain", () => {
    expect(validateCreateCase("Title", "normal", schema, { requester: "" })).toEqual({ requester: "Requester is required." });
  });
});
