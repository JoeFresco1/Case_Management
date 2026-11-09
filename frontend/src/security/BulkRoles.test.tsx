import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BulkRoles } from "./BulkRoles";

const roleId = "10000000-0000-0000-0000-000000000001";
const first = "20000000-0000-0000-0000-00000000000a";
const second = "20000000-0000-0000-0000-00000000000b";

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <BulkRoles />
    </QueryClientProvider>,
  );
}

describe("Bulk role assignments", () => {
  afterEach(() => vi.restoreAllMocks());

  it.each(["add", "remove"])(
    "previews %s operations and retries only failed users",
    async (operation) => {
      const attempts: string[] = [];
      vi.spyOn(globalThis, "fetch").mockImplementation(
        async (path, options) => {
          if (!options?.method || options.method === "GET")
            return Response.json([{ id: roleId, name: "Reviewer" }]);
          expect(options.method).toBe(operation === "add" ? "POST" : "DELETE");
          const user =
            operation === "add"
              ? JSON.parse(String(options.body)).user_id
              : String(path).split("/").pop();
          attempts.push(user);
          if (user === second && attempts.length === 2)
            return Response.json(
              { message: "Service unavailable" },
              { status: 503 },
            );
          return operation === "add"
            ? Response.json({ user_id: user, role_id: roleId })
            : new Response(null, { status: 204 });
        },
      );
      renderWorkspace();
      await screen.findByRole("option", { name: "Reviewer" });
      fireEvent.change(screen.getByLabelText("Role"), {
        target: { value: roleId },
      });
      fireEvent.change(screen.getByLabelText("Operation"), {
        target: { value: operation },
      });
      fireEvent.change(screen.getByLabelText("User IDs"), {
        target: { value: first + "," + second + "\n" + first.toUpperCase() },
      });
      fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
      expect(screen.getAllByRole("row")).toHaveLength(3);
      expect(attempts).toEqual([]);
      fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
      const retry = await screen.findByRole("button", { name: "Retry failed" });
      expect(
        within(screen.getByText(first).closest("tr")!).getByText("Applied"),
      ).toBeInTheDocument();
      expect(
        within(screen.getByText(second).closest("tr")!).getByText("Failed"),
      ).toBeInTheDocument();
      fireEvent.click(retry);
      expect(
        await screen.findByText("2 applied · 0 failed"),
      ).toBeInTheDocument();
      expect(attempts).toEqual([first, second, second]);
    },
  );

  it("rejects invalid UUIDs and batches over 50 unique users", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      Response.json([{ id: roleId, name: "Reviewer" }]),
    );
    renderWorkspace();
    await screen.findByRole("option", { name: "Reviewer" });
    fireEvent.change(screen.getByLabelText("Role"), {
      target: { value: roleId },
    });
    const users = screen.getByLabelText("User IDs");
    const submit = screen.getByRole("button", { name: "Preview changes" });
    fireEvent.change(users, { target: { value: "invalid" } });
    fireEvent.click(submit);
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid user ID");
    fireEvent.change(users, {
      target: {
        value: Array.from(
          { length: 51 },
          (_, index) =>
            "20000000-0000-0000-0000-" + String(index).padStart(12, "0"),
        ).join(","),
      },
    });
    fireEvent.click(submit);
    expect(screen.getByRole("alert")).toHaveTextContent("up to 50");
    expect(screen.queryByRole("button", { name: "Apply changes" })).toBeNull();
  });
});
