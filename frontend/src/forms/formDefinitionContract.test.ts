import { describe, expect, it, vi, afterEach } from "vitest";

import { api } from "../api/client";
import {
  isValidFormDefinition,
  validateFormDefinition,
} from "./formValidation";
import { toLocalDefinition } from "./FormRenderer";
import { toProgressEntries } from "./MultiStepForm";

/** Single-format + generated-shape contract (E-6 / FM-037 / P07-US5).
 *
 * The visual authoring surface must emit exactly the API definition the
 * runtime consumes: no second form format, no hand-typed API shape. These
 * tests pin that by round-tripping a designer payload through the shared
 * validator and the generated wire adapters.
 */
describe("form definition single-format contract", () => {
  afterEach(() => vi.restoreAllMocks());

  it("designer output validates and matches the generated wire keys", () => {
    // given a payload as the VisualFormDesigner emits it
    const designerOutput = {
      sections: [
        {
          key: "intake",
          label: "Intake",
          fields: [{ key: "title", label: "Title", required: true }],
          visible_when: null,
        },
        {
          key: "appeal",
          label: "Appeal",
          fields: [{ key: "appeal_reason", label: "Reason", required: true }],
          visible_when: { eq: { field: "title", value: "appeal" } },
        },
      ],
      validations: [
        {
          when: { eq: { field: "title", value: "appeal" } },
          then: { required: "appeal_reason" },
        },
      ],
    };
    // when validated + normalized through the renderer adapter
    const issues = validateFormDefinition(designerOutput);
    const local = toLocalDefinition({
      sections: designerOutput.sections,
      validations: designerOutput.validations as unknown as never,
    });
    // then it is valid, JSON-stable, and key-identical to the wire shape
    expect(issues).toEqual([]);
    expect(isValidFormDefinition(JSON.parse(JSON.stringify(designerOutput)))).toBe(true);
    expect(local.sections.map((s) => s.key)).toEqual(["intake", "appeal"]);
    expect(local.validations).toHaveLength(1);
  });

  it("wire progress tuples normalize deterministically", () => {
    // given generated progress tuples incl. an unknown status
    // when normalized
    const entries = toProgressEntries([
      ["basics", "completed"],
      ["review", "current"],
      ["extra", "bogus"],
    ]);
    // then statuses are deterministic and order-preserving
    expect(entries).toEqual([
      { step_key: "basics", status: "completed" },
      { step_key: "review", status: "current" },
      { step_key: "extra", status: "pending" },
    ]);
  });

  it("session clients hit the versioned E-6 paths with idempotency + concurrency", async () => {
    // given fetch spies over the generated session namespace
    const sessionBody = {
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      case_id: "case-1",
      form_version_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      actor_id: "actor-1",
      status: "in_progress",
      current_step_index: 0,
      version_token: 1,
      completed_steps: [],
      steps: [],
      progress: [],
      current_step: null,
      step_data: {},
      updated_at: null,
    };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(sessionBody), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    // when starting + saving through the typed client
    await api.forms.startSession("case-1", "Intake");
    await api.forms.saveStep("case-1", "sess-1", "basics", {
      step_key: "basics",
      data: { title: "x" },
      expected_version_token: 1,
    }, 1);
    // then paths are versioned and guarded by Idempotency-Key / If-Match
    const [startUrl, startInit] = fetchSpy.mock.calls[0];
    expect(String(startUrl)).toBe("/api/v1/forms/cases/case-1/sessions");
    expect(new Headers(startInit?.headers).get("Idempotency-Key")).toBeTruthy();
    const [saveUrl, saveInit] = fetchSpy.mock.calls[1];
    expect(String(saveUrl)).toBe(
      "/api/v1/forms/cases/case-1/sessions/sess-1/steps/basics",
    );
    expect(new Headers(saveInit?.headers).get("If-Match")).toBe('"1"');
  });
});
