import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskPage } from "./TaskList";
import type { TaskView } from "../api/client";

const TASK: TaskView = {
  id: "11111111-1111-1111-1111-111111111111",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  case_id: "33333333-3333-3333-3333-333333333333",
  type: "review",
  title: "Review synthetic case",
  status: "open",
  priority: "high",
  assignee_user_id: null,
  assignee_queue_id: "review_queue",
  due_at: null,
  completed_at: null,
  completed_by: null,
  outcome: null,
  version_token: 1,
};

function renderPage(path = "/tasks") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes><Route path="/tasks" element={<TaskPage />} /><Route path="/tasks/:taskId" element={<TaskPage />} /></Routes></MemoryRouter></QueryClientProvider>);
}

describe("TaskPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders filtered task list and sends assignee/status query parameters", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ items: [TASK], next_cursor: null, limit: 50 }), { status: 200 }));
    renderPage();
    await waitFor(() => expect(screen.getByText("Review synthetic case")).toBeInTheDocument());
    const url = String(fetchSpy.mock.calls[0][0]);
    expect(url).toContain("assignee=me");
    expect(url).toContain("status=open");
    expect(screen.getByText("review_queue", { exact: false })).toBeInTheDocument();
  });

  it("shows task detail and completes an open task with idempotency", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "POST") return new Response(JSON.stringify({ ...TASK, status: "completed", version_token: 2 }), { status: 200 });
      return new Response(JSON.stringify({ items: [TASK], next_cursor: null, limit: 200 }), { status: 200 });
    });
    renderPage(`/tasks/${TASK.id}`);
    await waitFor(() => expect(screen.getByRole("button", { name: "Complete task" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Complete task" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Task completed."));
    const post = fetchSpy.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(post?.[0])).toBe(`/api/v1/tasks/${TASK.id}/complete`);
    expect((post?.[1]?.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  });
});
