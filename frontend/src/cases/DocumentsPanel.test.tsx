import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentsPanel } from "./DocumentsPanel";
import { api } from "../api/client";

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DocumentsPanel caseId="case-1" />
    </QueryClientProvider>,
  );
}

const searchResponse = {
  items: [
    {
      id: "doc-1",
      case_id: "case-1",
      file_name: "evidence.pdf",
      classification: "Evidence",
      sensitivity: "restricted",
      current_version: 1,
      content_type: "application/pdf",
      sha256: "abc",
      uploaded_by: "user-1",
      uploaded_at: "2026-09-06T12:00:00Z",
      snippet: { redacted: true, reason: "sensitivity" },
    },
  ],
  next_cursor: null,
  limit: 50,
  total_considered: 1,
};

describe("DocumentsPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders metadata and does not expose a redacted preview", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify(searchResponse), { status: 200 }),
    );

    renderPanel();
    await waitFor(() => expect(screen.getByText("evidence.pdf")).toBeInTheDocument());
    expect(screen.getByText("Preview restricted")).toBeInTheDocument();
    expect(screen.queryByText(/sensitivity/)).not.toBeInTheDocument();
  });

  it("uploads the selected file as multipart metadata", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "idempotency-1" });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], limit: 50 }), { status: 200 }),
    );
    const uploadSpy = vi.spyOn(api, "uploadDocument").mockResolvedValueOnce({} as never);

    renderPanel();
    const file = new File(["synthetic content"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("File"), { target: { files: [file] } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Upload document" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(1));
    expect(uploadSpy).toHaveBeenCalledWith("case-1", file, {
      classification: "Correspondence",
      sensitivity: "internal",
    });
  });
});
