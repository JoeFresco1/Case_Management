import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RevisionHistory } from "./RevisionHistory";
import { CheckoutIndicator } from "./CheckoutIndicator";

function renderWithClient(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("test_viewer_history_<scenario>__<expectation>", () => {
  afterEach(() => vi.restoreAllMocks());

  it("test_revision_history_forbidden__renders_restricted_state", async () => {
    // given a restricted document
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ message: "forbidden" }), { status: 403 }),
    );

    // when history mounts
    renderWithClient(<RevisionHistory documentId="doc-x" />);

    // then the metadata-free state renders (no hashes leak)
    await waitFor(() =>
      expect(
        screen.getByText("Revision history is restricted or not available."),
      ).toBeInTheDocument(),
    );
  });

  it("test_revision_history_entries__renders_unified_timeline", async () => {
    // given a filed version plus a redacted burn
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          document_id: "doc-1",
          entries: [
            {
              entry_type: "version",
              version_number: 1,
              kind: "original",
              sha256: "aa".repeat(32),
              size: 10,
              occurred_at: "2026-09-06T12:00:00Z",
              rendition_id: null,
              redaction_id: null,
            },
            {
              entry_type: "rendition",
              version_number: 1,
              kind: "redacted",
              sha256: "bb".repeat(32),
              size: 11,
              occurred_at: "2026-09-06T12:05:00Z",
              rendition_id: "rend-1",
              redaction_id: "red-1",
            },
          ],
        }),
        { status: 200 },
      ),
    );

    // when history mounts
    renderWithClient(<RevisionHistory documentId="doc-1" />);

    // then both rows render oldest-first with kind badges
    await waitFor(() => expect(screen.getByText("Revision history")).toBeInTheDocument());
    expect(screen.getAllByText("v1")).toHaveLength(2);
    expect(screen.getByText("redacted")).toBeInTheDocument();
  });

  it("test_checkout_indicator_live__shows_holder_and_expiry", async () => {
    // given a live foreign checkout
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify([
          {
            id: "lock-1",
            document_id: "doc-1",
            holder_user_id: "user-9",
            acquired_at: "2026-09-06T12:00:00Z",
            expires_at: "2099-09-06T12:00:00Z",
            released_at: null,
            renewed: false,
          },
        ]),
        { status: 200 },
      ),
    );

    // when the indicator mounts
    renderWithClient(<CheckoutIndicator documentId="doc-1" />);

    // then the holder renders (safe metadata) with checkout actions
    await waitFor(() => expect(screen.getByText("Checked out")).toBeInTheDocument());
    expect(screen.getAllByText("user-9").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Check out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check in" })).toBeInTheDocument();
  });
});
