import { describe, expect, it } from "vitest";

import {
  type DesignerDefinition,
  type DesignerExportBody,
  type DesignerTransition,
  serializeDefinition,
  validateDesign,
} from "./VisualDesigner";

/**
 * Engine-format wire contract for the visual designer (FM-020 /
 * P10-US7). These tests pin the serialization shape and the
 * structural validation surface so that:
 *
 * - the export body matches `WorkflowDefinitionRequest` (backend
 *   `app/interfaces/http/schemas/admin_workflows.py`) byte-for-byte,
 * - a definition produced in the canvas validates cleanly against
 *   the engine's own rules and survives a round-trip to JSON.
 *
 * The backend `/api/v1/admin/workflows/{key}/versions` endpoint
 * accepts `DesignerExportBody` with **no translation** — that is the
 * "no second format" guarantee. If any of these tests change, the
 * backend schema must change too.
 */

const SAMPLE_DEFINITION: DesignerDefinition = {
  states: [
    {
      key: "intake",
      label: "Intake",
      is_initial: true,
      is_terminal: false,
      x: 10,
      y: 20,
    },
    {
      key: "in_review",
      label: "In review",
      is_initial: false,
      is_terminal: false,
      x: 200,
      y: 20,
    },
    {
      key: "needs_info",
      label: "Needs info",
      is_initial: false,
      is_terminal: false,
      x: 400,
      y: 20,
    },
    {
      key: "decided",
      label: "Decided",
      is_initial: false,
      is_terminal: false,
      x: 600,
      y: 20,
    },
    {
      key: "closed",
      label: "Closed",
      is_initial: false,
      is_terminal: true,
      x: 800,
      y: 20,
    },
  ],
  transitions: [
    {
      key: "submit_for_review",
      from_state: "intake",
      to_state: "in_review",
      human_driven: true,
      guard: null,
      actions: [],
    },
    {
      key: "request_info",
      from_state: "in_review",
      to_state: "needs_info",
      human_driven: true,
      guard: {
        op: "exists",
        field: "case.documents",
        min: 1,
      },
      actions: [
        { type: "create_task", task_type: "follow_up", queue: "intake" },
      ],
    },
    {
      key: "return_to_review",
      from_state: "needs_info",
      to_state: "in_review",
      human_driven: true,
      guard: null,
      actions: [],
    },
    {
      key: "decide",
      from_state: "in_review",
      to_state: "decided",
      human_driven: true,
      guard: null,
      actions: [],
    },
    {
      key: "close",
      from_state: "decided",
      to_state: "closed",
      human_driven: false,
      guard: null,
      actions: [{ type: "set_field", field: "closed_at" }],
    },
  ],
};

describe("VisualDesigner serialization", () => {
  it("exports the exact wire shape the engine consumes", () => {
    const exported: DesignerExportBody = serializeDefinition(SAMPLE_DEFINITION);

    // Top-level keys must be exactly { definition: { states, transitions } }
    expect(Object.keys(exported)).toEqual(["definition"]);
    expect(Object.keys(exported.definition).sort()).toEqual([
      "states",
      "transitions",
    ]);

    // Each state has only the four wire keys; UI coordinates MUST
    // not leak into the export.
    for (const state of exported.definition.states) {
      expect(Object.keys(state).sort()).toEqual([
        "is_initial",
        "is_terminal",
        "key",
        "label",
      ]);
    }

    // Each transition has only the six wire keys.
    for (const transition of exported.definition.transitions) {
      expect(Object.keys(transition).sort()).toEqual([
        "actions",
        "from_state",
        "guard",
        "human_driven",
        "key",
        "to_state",
      ]);
    }
  });

  it("round-trips through JSON.stringify without losing engine fields", () => {
    const exported = serializeDefinition(SAMPLE_DEFINITION);
    const roundTripped = JSON.parse(
      JSON.stringify(exported),
    ) as DesignerExportBody;
    expect(roundTripped).toEqual(exported);
  });

  it("produces a body the engine accepts without conversion", () => {
    // The backend DraftWorkflowVersionRequest schema accepts
    // `definition: WorkflowDefinitionRequest` where the request body
    // carries the same {states, transitions} arrays directly. The
    // serialized payload here is the literal body — no projection,
    // no translation layer. This test pins that invariant.
    const exported = serializeDefinition(SAMPLE_DEFINITION);
    expect(exported.definition.states).toHaveLength(
      SAMPLE_DEFINITION.states.length,
    );
    expect(exported.definition.transitions).toHaveLength(
      SAMPLE_DEFINITION.transitions.length,
    );

    // Spot-check the human-driven cycle shape used by the FM-020
    // acceptance ("a human-driven cycle is allowed").
    const cycle = exported.definition.transitions.filter((t) =>
      ["in_review", "needs_info"].includes(t.from_state) &&
      ["in_review", "needs_info"].includes(t.to_state),
    );
    expect(cycle).toHaveLength(2);
    expect(cycle.every((t) => t.human_driven)).toBe(true);
  });

  it("preserves guard and action documents verbatim", () => {
    const exported = serializeDefinition(SAMPLE_DEFINITION);
    const followUp = exported.definition.transitions.find(
      (t) => t.key === "request_info",
    );
    expect(followUp).toBeDefined();
    expect(followUp?.guard).toEqual({
      op: "exists",
      field: "case.documents",
      min: 1,
    });
    expect(followUp?.actions).toEqual([
      { type: "create_task", task_type: "follow_up", queue: "intake" },
    ]);
  });
});

describe("VisualDesigner structural validation", () => {
  it("accepts a well-formed definition", () => {
    const issues = validateDesign(SAMPLE_DEFINITION);
    expect(issues).toEqual([]);
  });

  it("rejects definitions without an initial state", () => {
    const broken: DesignerDefinition = {
      states: SAMPLE_DEFINITION.states.map((s) => ({
        ...s,
        is_initial: false,
      })),
      transitions: SAMPLE_DEFINITION.transitions,
    };
    const issues = validateDesign(broken);
    expect(issues.some((i) => i.code === "no_initial_state")).toBe(true);
  });

  it("rejects duplicate state keys", () => {
    const broken: DesignerDefinition = {
      states: [
        ...SAMPLE_DEFINITION.states,
        {
          key: "closed",
          label: "Closed again",
          is_initial: false,
          is_terminal: true,
          x: 0,
          y: 0,
        },
      ],
      transitions: SAMPLE_DEFINITION.transitions,
    };
    const issues = validateDesign(broken);
    expect(
      issues.some(
        (i) => i.code === "duplicate_state_key" && i.field === "states.closed",
      ),
    ).toBe(true);
  });

  it("rejects transitions whose from_state references a terminal", () => {
    const broken: DesignerDefinition = {
      states: SAMPLE_DEFINITION.states,
      transitions: [
        ...SAMPLE_DEFINITION.transitions,
        {
          key: "impossible",
          from_state: "closed",
          to_state: "intake",
          human_driven: false,
          guard: null,
          actions: [],
        },
      ],
    };
    const issues = validateDesign(broken);
    expect(
      issues.some(
        (i) =>
          i.code === "transition_from_terminal" && i.field.includes("impossible"),
      ),
    ).toBe(true);
  });

  it("rejects self-loops and references to undeclared states", () => {
    const selfLoop: DesignerTransition = {
      key: "loop",
      from_state: "intake",
      to_state: "intake",
      human_driven: true,
      guard: null,
      actions: [],
    };
    const dangling: DesignerTransition = {
      key: "to_nowhere",
      from_state: "intake",
      to_state: "ghost",
      human_driven: true,
      guard: null,
      actions: [],
    };
    const issues = validateDesign({
      states: SAMPLE_DEFINITION.states,
      transitions: [selfLoop, dangling],
    });
    expect(issues.some((i) => i.code === "transition_self_loop")).toBe(true);
    expect(issues.some((i) => i.code === "transition_unknown_to")).toBe(true);
  });
});
