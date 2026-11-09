import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HomeView } from "./HomeView";

const TENANT = "11111111-1111-1111-1111-111111111111";
const USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

function renderWithClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <HomeView />
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("HomeView (C-7 worker UX)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("adds a returned case to favorites without entering a case ID", async () => {
    const id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const dashboard = {
      user_id: USER,
      tenant_id: TENANT,
      widgets: [],
      favorites: [] as string[],
      shortcuts: [{ label: "Review queue", target: "/queues" }],
      grouping: "queue",
      sort: "due",
      task_pane_case_id: null,
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/cases?"))
        return jsonResponse({
          items: [
            {
              id,
              title: "Sample request",
              reference: "C-DEMO-001",
              status: "open",
            },
          ],
        });
      if (url.includes("/home/dashboard")) {
        if (init?.method === "PUT")
          dashboard.favorites = JSON.parse(String(init.body)).favorites;
        return jsonResponse(dashboard);
      }
      if (url.includes("/effective")) return jsonResponse({ preferences: [] });
      if (url.includes("/preference-policy"))
        return jsonResponse({ locked: {}, allowed: {} });
      return jsonResponse({ values: {} });
    });

    renderWithClient();
    expect(
      await screen.findByRole("link", { name: /Sample request/ }),
    ).toHaveAttribute("href", `/cases/${id}`);
    expect(
      await screen.findByRole("link", { name: "Review queue" }),
    ).toHaveAttribute("href", "/queues");
    fireEvent.click(screen.getByRole("button", { name: "Add to favorites" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Favorited" })).toBeDisabled(),
    );
    expect(dashboard.favorites).toEqual([id]);
  });

  it("test_home_admin_policy__overrides_user_preference__shows_locked_admin_value", async () => {
    // // given stored user values conflicting with an admin lock
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/home/dashboard")) {
        return jsonResponse({
          user_id: USER,
          tenant_id: TENANT,
          widgets: ["pinned_cases"],
          favorites: [],
          shortcuts: [],
          grouping: "queue",
          sort: "due",
          task_pane_case_id: null,
        });
      }
      if (url.includes("/home/preferences/effective")) {
        // // when effective merges // then the admin value wins, locked.
        return jsonResponse({
          user_id: USER,
          tenant_id: TENANT,
          preferences: [
            { key: "theme", value: "light", locked: true },
            { key: "density", value: "comfortable", locked: false },
            { key: "default_grouping", value: "none", locked: false },
            { key: "default_sort", value: "recent", locked: false },
            { key: "task_pane_side", value: "right", locked: false },
            { key: "recent_limit", value: "10", locked: false },
            { key: "page_size", value: "50", locked: false },
          ],
        });
      }
      if (url.includes("/home/preferences")) {
        return jsonResponse({
          user_id: USER,
          tenant_id: TENANT,
          values: { theme: "dark" },
        });
      }
      if (url.includes("/admin/preference-policy")) {
        return jsonResponse({
          tenant_id: TENANT,
          locked: { theme: "light" },
          allowed: {},
          updated_by: USER,
        });
      }
      return jsonResponse({}, 404);
    });

    renderWithClient();

    // // then the locked admin value renders with its badge.
    await waitFor(() =>
      expect(
        screen.getByLabelText("theme locked by admin"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/theme=light/)).toBeInTheDocument();
    const themeSelect = screen.getByLabelText(
      "Preference theme",
    ) as HTMLSelectElement;
    expect(themeSelect.value).toBe("light");
    expect(themeSelect.disabled).toBe(true);
  });
});
