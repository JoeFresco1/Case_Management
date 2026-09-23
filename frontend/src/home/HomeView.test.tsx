import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
      expect(screen.getByLabelText("theme locked by admin")).toBeInTheDocument(),
    );
    expect(screen.getByText(/theme=light/)).toBeInTheDocument();
    const themeSelect = screen.getByLabelText(
      "Preference theme",
    ) as HTMLSelectElement;
    expect(themeSelect.value).toBe("light");
    expect(themeSelect.disabled).toBe(true);
  });
});
