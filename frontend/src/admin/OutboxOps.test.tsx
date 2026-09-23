import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OutboxOps } from "./OutboxOps";

function renderOps() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OutboxOps />
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

const outboxFixture = {
  items: [
    {
      id: "row-1",
      tenant_id: "tenant-1",
      event_type: "CaseCreated",
      event_id: "event-1",
      created_at: "2026-09-01T00:00:00Z",
      dispatched_at: null,
      attempt_count: 5,
      last_error: "connection refused",
      is_dead_lettered: true,
    },
  ],
  limit: 50,
  lag: { total: 1, pending: 1, dead_lettered: 1, retrying: 1 },
};

const builtinCatalogFixture = {
  items: [
    {
      event_type: "CaseCreated",
      version: 1,
      description: "A case was created",
      payload_keys: ["case_id", "reference"],
    },
  ],
  next_cursor: null,
  limit: 50,
  source: "EVENT_CATALOG",
};

const extensionCatalogFixture = {
  items: [
    {
      event_type: "CaseCreated",
      version: 1,
      description: "Narrowed duplicate",
      payload_keys: ["case_id"],
    },
    {
      event_type: "CustomExported",
      version: 1,
      description: "Extension-only event",
      payload_keys: ["bundle_id"],
    },
  ],
  source: "EVENT_CATALOG+ExtensionRegistry",
};

const subscriptionsFixture = {
  items: [
    {
      id: "sub-1",
      tenant_id: "tenant-1",
      url: "https://example.test/hook",
      event_filters: ["CaseCreated"],
      secret_name: "hook-secret",
      signing_algorithm: "hmac_sha256",
      require_bearer: false,
      status: "active",
      version_token: 1,
      description: "Example hook",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
      correlation_id: "",
    },
  ],
  next_cursor: null,
  limit: 50,
};

const diagnosticsFixture = {
  cache: { entries: 3 },
  services_desired: { outbox: true, sla_scanner: true },
  services_actual: { outbox: true, sla_scanner: false },
  synthetic_environment: true,
};

const storageFixture = {
  declared_volume: { adapter: "local-fs", tier: "standard" },
  declared_encryption: { enabled: false },
  actual_volume: { adapter: "local-fs" },
  actual_encryption: { adapter: "local-plaintext", enabled: false },
};

function mockOpsApi(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    "/api/v1/admin/outbox": outboxFixture,
    "/api/v1/events/catalog": builtinCatalogFixture,
    "/api/v1/extensions/events/catalog": extensionCatalogFixture,
    "/api/v1/webhooks/subscriptions": subscriptionsFixture,
    "/api/v1/admin/system/diagnostics": diagnosticsFixture,
    "/api/v1/admin/system/storage": storageFixture,
    ...overrides,
  };
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/admin/outbox/row-1/replay")) {
      return Promise.resolve(jsonResponse({ id: "row-1", reset: true }));
    }
    if (url.includes("/subscriptions/sub-1/replay")) {
      return Promise.resolve(
        jsonResponse({ ...subscriptionsFixture.items[0] }, 202),
      );
    }
    for (const [path, body] of Object.entries(routes)) {
      if (url.includes(path) && body instanceof Response) {
        return Promise.resolve(body);
      }
      if (url.includes(path)) {
        return Promise.resolve(jsonResponse(body));
      }
    }
    return Promise.resolve(jsonResponse({ message: "not found" }, 404));
  });
}

describe("OutboxOps", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders failed-work lag, the pending row, and its last error", async () => {
    // given the full ops backend
    mockOpsApi();
    // when the console mounts
    renderOps();
    // then lag, row, and safe last-error are visible (CaseCreated shows
    // both in the failed-work row and in the event-catalog section)
    await waitFor(() =>
      expect(screen.getAllByText("CaseCreated").length).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByText(/connection refused/)).toBeInTheDocument();
    expect(screen.getByText(/Pending:/)).toBeInTheDocument();
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(4);
  });

  it("requires confirmation before replaying a failed-work row", async () => {
    // given the console with one dead-lettered row
    const fetchSpy = mockOpsApi();
    renderOps();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Replay CaseCreated/ }),
      ).toBeInTheDocument(),
    );
    // when the operator arms the replay but has not confirmed
    fireEvent.click(screen.getByRole("button", { name: /Replay CaseCreated/ }));
    // then no replay POST has been sent yet
    expect(
      fetchSpy.mock.calls.some(([url]) => String(url).includes("/replay")),
    ).toBe(false);
    expect(
      screen.getByRole("button", { name: "Confirm replay" }),
    ).toBeInTheDocument();
    // when the operator confirms
    fireEvent.click(screen.getByRole("button", { name: "Confirm replay" }));
    // then the reset confirmation is announced
    await waitFor(() =>
      expect(screen.getByText(/Replay armed for row row-1/)).toBeInTheDocument(),
    );
    const replayCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/admin/outbox/row-1/replay"),
    );
    expect(replayCall?.[1]).toMatchObject({ method: "POST" });
  });

  it("cancels an armed replay without contacting the backend", async () => {
    // given the console with one dead-lettered row
    const fetchSpy = mockOpsApi();
    renderOps();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Replay CaseCreated/ }),
      ).toBeInTheDocument(),
    );
    // when the operator arms then cancels
    fireEvent.click(screen.getByRole("button", { name: /Replay CaseCreated/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    // then no replay POST is sent and the arm UI closes
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Confirm replay" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      fetchSpy.mock.calls.some(([url]) => String(url).includes("/replay")),
    ).toBe(false);
  });

  it("surfaces safe section errors without blanking the other sections", async () => {
    // given the outbox seam is down but every other seam answers
    mockOpsApi({
      "/api/v1/admin/outbox": jsonResponse({ message: "outbox unavailable" }, 503),
    });
    // when the console mounts
    renderOps();
    // then the safe message renders as an alert and the catalog still loads
    await waitFor(() =>
      expect(screen.getByText("outbox unavailable")).toBeInTheDocument(),
    );
    expect(screen.getByText("CustomExported")).toBeInTheDocument();
  });

  it("flags extension entries that narrow a built-in version", async () => {
    // given an extension overlay that drops a built-in payload key
    mockOpsApi();
    // when the console mounts
    renderOps();
    // then the breaking change is flagged with the removed key
    await waitFor(() =>
      expect(screen.getByText(/Breaking change/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/removed reference/)).toBeInTheDocument();
    // and the compatible extension-only entry is still listed
    expect(screen.getByText("CustomExported")).toBeInTheDocument();
  });

  it("replays dead-lettered webhook deliveries with an idempotency key", async () => {
    // given one active subscription
    const fetchSpy = mockOpsApi();
    renderOps();
    await waitFor(() =>
      expect(screen.getByText("https://example.test/hook")).toBeInTheDocument(),
    );
    // when the operator confirms the subscription replay
    fireEvent.click(
      screen.getByRole("button", { name: /Replay dead-lettered/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm replay" }));
    // then the replay POST carries Idempotency-Key and the audit note shows
    await waitFor(() =>
      expect(
        screen.getByText(/Dead-lettered deliveries replay requested/),
      ).toBeInTheDocument(),
    );
    const replayCall = fetchSpy.mock.calls.find(([url]) =>
      String(url).includes("/subscriptions/sub-1/replay"),
    );
    expect(replayCall?.[1]).toMatchObject({ method: "POST" });
    const headers = (replayCall?.[1] as RequestInit).headers as Record<
      string,
      string
    >;
    expect(headers["Idempotency-Key"]).toBeTruthy();
  });

  it("renders desired-vs-actual diagnostics and the synthetic marker", async () => {
    // given diagnostics where the SLA scanner is desired but down
    mockOpsApi();
    // when the console mounts
    renderOps();
    // then the mismatch and synthetic marker are visible with config impact
    await waitFor(() =>
      expect(screen.getByText("sla_scanner")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Desired and actual differ/)).toBeInTheDocument();
    expect(screen.getByText("yes")).toBeInTheDocument();
    expect(screen.getByText(/Key material is never shown here/)).toBeInTheDocument();
  });
});
