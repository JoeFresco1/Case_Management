import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { CaseSummary } from "../api/types";
import { TransitionsPanel } from "./TransitionsPanel";

const baseCase: CaseSummary = {
  id: "case-1",
  tenant_id: "tenant-1",
  reference: "C-ACME-2026-000001",
  case_type_key: "ServiceRequest",
  case_type_version_id: "version-1",
  status: "open",
  priority: "normal",
  title: "Synthetic request",
  description: "",
  data: {},
  current_workflow_state: "intake",
  version_token: 1,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  created_by: "user-1",
  closed_at: null,
  assignee: null,
  queue: null,
  sla_state: null,
  participants: [],
  open_tasks: [],
  recent_documents: [],
  recent_decisions: [],
  timeline: [],
  legal_hold: false,
};

function renderPanel(caseSummary = baseCase) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TransitionsPanel caseSummary={caseSummary} />
    </QueryClientProvider>,
  );
}

describe("TransitionsPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows a blocked document guard and does not execute it", async () => {
    vi.spyOn(api, "listTransitions").mockResolvedValue({
      case_id: "case-1",
      workflow_instance_id: "instance-1",
      workflow_version_id: "version-1",
      current_state: "intake",
      transitions: [
        {
          key: "submit_for_review",
          from_state: "intake",
          to_state: "in_review",
          allowed: false,
          reason: "Requires a Contract document",
          details: null,
          approval_required: false,
          approval_eligible: false,
        },
      ],
    });
    const executeSpy = vi.spyOn(api, "executeTransition");
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Guard: blocked/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Apply transition" })).toBeDisabled();
    });
    expect(executeSpy).not.toHaveBeenCalled();
  });

  it("executes a satisfied transition and reports the resulting state", async () => {
    vi.spyOn(api, "listTransitions").mockResolvedValue({
      case_id: "case-1",
      workflow_instance_id: "instance-1",
      workflow_version_id: "version-1",
      current_state: "intake",
      transitions: [
        {
          key: "submit_for_review",
          from_state: "intake",
          to_state: "in_review",
          allowed: true,
          reason: null,
          details: null,
          approval_required: false,
          approval_eligible: false,
        },
      ],
    });
    vi.spyOn(api, "executeTransition").mockResolvedValue({
      case_id: "case-1",
      from_state: "intake",
      to_state: "in_review",
      transition_key: "submit_for_review",
      created_tasks: [],
    });
    renderPanel({
      ...baseCase,
      recent_documents: [{ classification: "Contract" }],
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "Apply transition" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Apply transition" }));
    await waitFor(() => expect(api.executeTransition).toHaveBeenCalledWith("case-1", "submit_for_review"));
    expect(await screen.findByRole("status")).toHaveTextContent("intake → in_review");
  });
});
