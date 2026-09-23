import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentViewer } from "./DocumentViewer";

function renderViewer(documentId = "doc-1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DocumentViewer documentId={documentId} />
    </QueryClientProvider>,
  );
}

const versionsResponse = [
  { version_number: 1, sha256: "aa".repeat(32), size: 10, content_type: "text/plain" },
  { version_number: 2, sha256: "bb".repeat(32), size: 12, content_type: "text/plain" },
];

const annotationsResponse = [
  {
    id: "ann-1",
    tenant_id: "tenant-1",
    document_id: "doc-1",
    document_version: 2,
    page: 1,
    kind: "highlight",
    geometry: { x: 0.1 },
    content: null,
    created_by: "user-1",
    created_at: "2026-09-06T12:00:00Z",
  },
  {
    id: "ann-2",
    tenant_id: "tenant-1",
    document_id: "doc-1",
    document_version: 2,
    page: 2,
    kind: "text_box",
    geometry: { x: 0.2 },
    content: "review this",
    created_by: "user-1",
    created_at: "2026-09-06T12:01:00Z",
  },
];

describe("test_document_viewer_<mode>__<expectation>", () => {
  afterEach(() => vi.restoreAllMocks());

  it("test_document_viewer_forbidden__renders_metadata_free_state", async () => {
    // given a restricted document (versions 403, annotations 404)
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/versions")) {
        return Promise.resolve(
          new Response(JSON.stringify({ message: "forbidden" }), { status: 403 }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ message: "not found" }), { status: 404 }),
      );
    });

    // when the viewer mounts with a tempting file name nearby
    renderViewer("doc-restricted");

    // then only the generic forbidden state renders (no ids, hashes, captions)
    await waitFor(() =>
      expect(
        screen.getByText("Document is restricted or not available."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("review this")).not.toBeInTheDocument();
    expect(screen.queryByText(/ann-1/)).not.toBeInTheDocument();
    expect(screen.queryByText(/evidence\.pdf/)).not.toBeInTheDocument();
  });

  it("test_document_viewer_paged__shows_thumbnails_and_detail", async () => {
    // given a document with two versions and marks on pages 1-2
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/versions")) {
        return Promise.resolve(new Response(JSON.stringify(versionsResponse), { status: 200 }));
      }
      if (url.includes("/annotations")) {
        return Promise.resolve(
          new Response(JSON.stringify(annotationsResponse), { status: 200 }),
        );
      }
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    });

    // when the viewer mounts in paged mode
    renderViewer("doc-1");

    // then the mode switch, page strip, and version selector render
    await waitFor(() => expect(screen.getByLabelText("Viewer mode")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "paged" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Page 1, 1 annotations")).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Page 2, 1 annotations")).toBeInTheDocument();
    expect(screen.getByLabelText("Annotation version filter")).toBeInTheDocument();
  });
});
