import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BundleSharing } from "./BundleSharing";

function renderSharing() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BundleSharing initialBundleId="bundle-1" />
    </QueryClientProvider>,
  );
}

describe("test_bundle_sharing_<scenario>__<expectation>", () => {
  afterEach(() => vi.restoreAllMocks());

  it("test_bundle_sharing_forbidden__hides_name_and_members", async () => {
    // given a bundle the actor may not see
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ message: "forbidden" }), { status: 403 }),
    );

    // when the sharing console opens that bundle
    renderSharing();

    // then only the generic state renders (no name/member leak)
    await waitFor(() =>
      expect(
        screen.getByText("Bundle is restricted or not available."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/secret-bundle/)).not.toBeInTheDocument();
    expect(screen.queryByText(/doc-/)).not.toBeInTheDocument();
  });

  it("test_bundle_sharing_visible__shows_metadata_and_mint_form", async () => {
    // given a granted bundle
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          id: "bundle-1",
          tenant_id: "tenant-1",
          name: "Hearing prep",
          member_document_ids: ["doc-1", "doc-2"],
          status: "active",
          created_by: "user-1",
        }),
        { status: 200 },
      ),
    );

    // when the console opens it
    renderSharing();

    // then metadata plus the share-by-link form render
    await waitFor(() => expect(screen.getByText("Hearing prep")).toBeInTheDocument());
    expect(screen.getByText("doc-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mint link" })).toBeInTheDocument();
  });
});
