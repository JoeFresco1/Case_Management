import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RoleAdmin } from "./RoleAdmin";

function renderAdmin() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RoleAdmin />
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200 });
}

const roleFixture = {
  id: "role-1",
  tenant_id: "tenant-1",
  name: "reviewer",
  description: "Can review cases",
  permissions: ["CASE_READ"],
  is_reserved: false,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

describe("RoleAdmin", () => {
  afterEach(() => vi.restoreAllMocks());

  it("lists roles with permissions", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse([roleFixture]));
    renderAdmin();
    await waitFor(() => expect(screen.getByText("reviewer")).toBeInTheDocument());
    expect(screen.getByText("CASE_READ")).toBeInTheDocument();
  });

  it("defines a role with parsed permissions over the wire", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse(roleFixture))
      .mockResolvedValueOnce(jsonResponse([roleFixture]));
    renderAdmin();
    await waitFor(() => expect(screen.getByText("Define a role")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "reviewer" } });
    fireEvent.change(screen.getByLabelText("Permissions (comma-separated)"), {
      target: { value: "CASE_READ, CASE_WRITE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Define role" }));
    // list, POST define, then the success-invalidation list refetch
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(3));
    const [url, init] = fetchSpy.mock.calls[1] as [string, RequestInit];
    expect(url).toContain("/api/v1/admin/roles");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      name: "reviewer",
      description: "",
      permissions: ["CASE_READ", "CASE_WRITE"],
    });
  });
});
