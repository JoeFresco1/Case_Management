import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QueueAdmin } from "./QueueAdmin";

function renderAdmin() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <QueueAdmin />
    </QueryClientProvider>,
  );
}

describe("QueueAdmin", () => {
  afterEach(() => vi.restoreAllMocks());

  it("sends an idempotent queue mutation without fabricated identity headers", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    renderAdmin();
    await screen.findByText("No queues yet.");

    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "review_queue" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Review queue" } });
    fireEvent.click(screen.getByRole("button", { name: "Define queue" }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(3));
    const [url, init] = fetchSpy.mock.calls[1] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(url).toBe("/api/v1/admin/queues");
    expect(init.method).toBe("POST");
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(headers.has("X-Dev-User")).toBe(false);
    expect(headers.has("X-Dev-Tenant")).toBe(false);
  });
});
