import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import { FormRenderer } from "./FormRenderer";

type WireVersion = components["schemas"]["FormVersionResponse"];

const publishedVersion: WireVersion = {
  id: "11111111-1111-1111-1111-111111111111",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  key: "Intake",
  version_number: 3,
  definition: {
    sections: [
      {
        key: "basics",
        label: "Basics",
        fields: [{ key: "title", label: "Title", required: true }],
        visible_when: null,
      },
      {
        key: "extra",
        label: "Extra",
        fields: [{ key: "notes", label: "Notes", required: true }],
        visible_when: { eq: { field: "title", value: "show" } },
      },
    ],
    validations: [],
  },
  status: "published",
  published_at: "2026-08-31T12:00:00+00:00",
  published_by: "admin",
  created_at: "2026-08-31T12:00:00+00:00",
};

function renderRenderer() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <FormRenderer caseId="case-1" formKey="Intake" />
    </QueryClientProvider>,
  );
}

describe("FormRenderer published rendering", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the published version with visible fields only", async () => {
    // given the published E-6 contract
    vi.spyOn(api, "getFormVersion").mockResolvedValue(publishedVersion);
    // when mounted
    renderRenderer();
    // then the published marker + visible field render; hidden section stays out
    expect(await screen.findByTestId("form-version")).toHaveTextContent(
      "Intake · version 3 · published",
    );
    expect(screen.getByLabelText("Title")).toBeInTheDocument();
    expect(screen.queryByLabelText("Notes")).not.toBeInTheDocument();
    expect(screen.getByText(/1 hidden answer.*notes/)).toBeInTheDocument();
  });

  it("names the missing field without losing entered data", async () => {
    // given a published form and a worker who typed a title
    vi.spyOn(api, "getFormVersion").mockResolvedValue({
      ...publishedVersion,
      definition: {
        sections: [
          {
            key: "basics",
            label: "Basics",
            fields: [
              { key: "title", label: "Title", required: true },
              { key: "summary", label: "Summary", required: true },
            ],
            visible_when: null,
          },
        ],
        validations: [],
      },
    });
    const submit = vi.spyOn(api, "submitForm").mockResolvedValue({
      case_id: "case-1",
      reference: "C-ACME-2026-000001",
      version_token: 2,
      hidden_keys: [],
    });
    renderRenderer();
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "kept" } });
    // when submitting with Summary empty
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    // then the deterministic field-named error shows and typing is preserved
    expect(await screen.findByRole("alert")).toHaveTextContent("Form field summary is required.");
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("kept");
    expect(submit).not.toHaveBeenCalled();
  });

  it("submits when a hidden required field is absent", async () => {
    // given the conditional form with Extra hidden
    vi.spyOn(api, "getFormVersion").mockResolvedValue(publishedVersion);
    const submit = vi.spyOn(api, "submitForm").mockResolvedValue({
      case_id: "case-1",
      reference: "C-ACME-2026-000001",
      version_token: 2,
      hidden_keys: ["notes"],
    });
    renderRenderer();
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "other" } });
    // when submitting (notes hidden + required)
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    // then the submit succeeds — hidden required never blocks
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  });

  it("preserves entered data on a deterministic server rejection", async () => {
    // given a published form whose server rejects the payload
    vi.spyOn(api, "getFormVersion").mockResolvedValue(publishedVersion);
    vi.spyOn(api, "submitForm").mockRejectedValue(
      new ApiClientError("title already taken", 422, "validation_failed"),
    );
    renderRenderer();
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "kept" } });
    // when submitting a visible, valid-looking payload
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    // then the server message shows and typing is preserved
    expect(await screen.findByRole("alert")).toHaveTextContent("title already taken");
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("kept");
  });

  it("links errors to fields for assistive technology", async () => {
    // given a published form submitted empty
    vi.spyOn(api, "getFormVersion").mockResolvedValue(publishedVersion);
    vi.spyOn(api, "submitForm").mockResolvedValue({
      case_id: "case-1",
      reference: "C-ACME-2026-000001",
      version_token: 2,
      hidden_keys: [],
    });
    renderRenderer();
    await screen.findByLabelText("Title");
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    // then the summary alert links to the field input
    const alert = await screen.findByRole("alert");
    const link = alert.querySelector("a");
    expect(link?.getAttribute("href")).toBe("#case-1-title");
    expect(screen.getByLabelText("Title")).toHaveAttribute("aria-invalid", "true");
  });
});
