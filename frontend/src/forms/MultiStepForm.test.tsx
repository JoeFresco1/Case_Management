import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import { MultiStepForm } from "./MultiStepForm";

type WireVersion = components["schemas"]["FormVersionResponse"];
type WireSession = components["schemas"]["FormSessionResponse"];

const version: WireVersion = {
  id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  key: "Intake",
  version_number: 1,
  definition: {
    sections: [
      {
        key: "basics",
        label: "Basics",
        fields: [{ key: "title", label: "Title", required: true }],
        visible_when: null,
      },
      {
        key: "review",
        label: "Review",
        fields: [{ key: "decision", label: "Decision", required: true }],
        visible_when: null,
      },
    ],
    validations: [],
  },
  status: "published",
  published_at: "2026-08-31T12:00:00+00:00",
  published_by: "admin",
  created_at: "2026-08-31T12:00:00+00:00",
};

function sessionFixture(overrides: Partial<WireSession> = {}): WireSession {
  return {
    id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    case_id: "case-1",
    form_version_id: version.id,
    actor_id: "actor-1",
    status: "in_progress",
    current_step_index: 0,
    version_token: 2,
    completed_steps: [],
    steps: [
      { step_key: "basics", label: "Basics", section_keys: ["basics"], required: true },
      { step_key: "review", label: "Review", section_keys: ["review"], required: true },
    ],
    progress: [["basics", "current"], ["review", "pending"]],
    current_step: {
      step_key: "basics",
      label: "Basics",
      section_keys: ["basics"],
      required: true,
    },
    step_data: { title: "draft" },
    updated_at: null,
    ...overrides,
  };
}

function renderMultiStep() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MultiStepForm caseId="case-1" formKey="Intake" />
    </QueryClientProvider>,
  );
}

describe("MultiStepForm resume", () => {
  afterEach(() => vi.restoreAllMocks());

  it("resumes persisted progress with answers intact", async () => {
    // given an open session with one drafted answer
    vi.spyOn(api.forms, "getVersion").mockResolvedValue(version);
    vi.spyOn(api.forms, "resumeSession").mockResolvedValue(sessionFixture());
    // when mounted
    renderMultiStep();
    // then the current step renders the persisted answer and progress marks it
    expect(await screen.findByLabelText("Title")).toHaveValue("draft");
    const progress = screen.getByRole("list", { name: "Form progress" });
    expect(progress).toBeInTheDocument();
    const current = screen.getByRole("listitem", { current: "step" });
    expect(current).toHaveTextContent("basics");
  });

  it("starts a new session when resume finds none", async () => {
    // given no open session (404) followed by a fresh start
    vi.spyOn(api.forms, "getVersion").mockResolvedValue(version);
    vi.spyOn(api.forms, "resumeSession").mockRejectedValue(
      new ApiClientError("not found", 404, "not_found"),
    );
    const start = vi.spyOn(api.forms, "startSession").mockResolvedValue(sessionFixture());
    // when mounted
    renderMultiStep();
    // then exactly one start issues and the step renders
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(await screen.findByLabelText("Title")).toBeInTheDocument();
  });

  it("blocks an empty required step without losing typing or calling the API", async () => {
    // given the first step with no answers yet
    vi.spyOn(api.forms, "getVersion").mockResolvedValue(version);
    vi.spyOn(api.forms, "resumeSession").mockResolvedValue(
      sessionFixture({ step_data: {}, version_token: 1 }),
    );
    const save = vi.spyOn(api.forms, "saveStep");
    renderMultiStep();
    await screen.findByLabelText("Title");
    // when saving empty
    fireEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    // then the field-named error shows and no save issues
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Form field title is required before continuing.",
    );
    expect(save).not.toHaveBeenCalled();
  });

  it("preserves answers on a stale-token conflict", async () => {
    // given a session whose save hits a concurrent writer
    vi.spyOn(api.forms, "getVersion").mockResolvedValue(version);
    vi.spyOn(api.forms, "resumeSession").mockResolvedValue(sessionFixture());
    vi.spyOn(api.forms, "saveStep").mockRejectedValue(
      new ApiClientError("stale", 409, "conflict"),
    );
    renderMultiStep();
    const input = (await screen.findByLabelText("Title")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "kept" } });
    // when saving against the stale token
    fireEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    // then the conflict message shows and typing is preserved
    expect(await screen.findByRole("alert")).toHaveTextContent(/preserved/);
    expect(input.value).toBe("kept");
  });
});
