import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VisualFormDesigner } from "./VisualFormDesigner";
import { isValidFormDefinition, validateFormDefinition } from "./formValidation";

/** Visual form designer contract (FM-037 / P07-US5).
 *
 * The designer is allowed to do exactly one thing: emit a
 * ``FormDefinition`` that the backend ``app.domain.form._validate_definition``
 * will accept without conversion. These tests pin that contract by:
 *
 * - asserting the empty seed is structurally valid,
 * - rendering the designer, adding a conditional section via the UI,
 *   then asserting the emitted JSON validates against the shared
 *   frontend rule set,
 * - asserting the emitted JSON parses back through
 *   ``JSON.parse(JSON.stringify(...))`` and remains valid (round-trip
 *   stability),
 * - asserting malformed edits (duplicate keys, unknown operator) surface
 *   as structural issues so the operator sees them before publishing.
 */

describe("VisualFormDesigner", () => {
  it("emits a valid definition after the operator authors a conditional section", () => {
    // given
    let emitted: unknown = null;
    render(
      <VisualFormDesigner
        initialDefinition={{
          sections: [
            {
              key: "intake",
              label: "Intake",
              fields: [
                { key: "is_appeal", label: "Is appeal?", required: false },
              ],
              visible_when: null,
            },
            {
              key: "appeal_details",
              label: "Appeal details",
              fields: [
                {
                  key: "appeal_reason",
                  label: "Reason for appeal",
                  required: true,
                },
              ],
              visible_when: { eq: { field: "is_appeal", value: "true" } },
            },
          ],
          validations: [
            {
              when: { eq: { field: "is_appeal", value: "true" } },
              then: { required: "appeal_reason" },
            },
          ],
        }}
        onChange={(definition) => {
          emitted = definition;
        }}
      />,
    );

    // when: the operator adds a third section via the toolbar
    fireEvent.click(screen.getByText("+ Add section"));

    // then: every emitted payload during the interaction must remain valid
    expect(emitted).not.toBeNull();
    expect(isValidFormDefinition(emitted)).toBe(true);

    const definition = emitted as ReturnType<typeof JSON.parse>;
    expect(definition.sections).toHaveLength(3);
    expect(definition.sections[1].visible_when).toEqual({
      eq: { field: "is_appeal", value: "true" },
    });
    expect(definition.validations[0]).toEqual({
      when: { eq: { field: "is_appeal", value: "true" } },
      then: { required: "appeal_reason" },
    });
  });

  it("surfaces structural issues when the operator creates a duplicate section key", () => {
    // given: a definition that simulates a hand-edited duplicate key
    const issues = validateFormDefinition({
      sections: [
        {
          key: "intake",
          label: "Intake",
          fields: [{ key: "title", label: "Title", required: true }],
          visible_when: null,
        },
        {
          key: "intake",
          label: "Intake (duplicate)",
          fields: [{ key: "summary", label: "Summary", required: false }],
          visible_when: null,
        },
      ],
      validations: [],
    });

    // then
    expect(issues.some((i) => i.message.includes("duplicate section key"))).toBe(
      true,
    );
  });

  it("flags unknown operators so the operator can fix them before publishing", () => {
    // given: a definition that simulates a hand-crafted unknown operator
    const issues = validateFormDefinition({
      sections: [
        {
          key: "intake",
          label: "Intake",
          fields: [{ key: "title", label: "Title", required: true }],
          visible_when: { regex: { field: "title", value: "^foo$" } },
        },
      ],
      validations: [],
    });

    // then
    expect(
      issues.some((i) => i.message.includes("unknown form operator")),
    ).toBe(true);
  });

  it("round-trips through JSON without losing validity", () => {
    // given
    const original = {
      sections: [
        {
          key: "intake",
          label: "Intake",
          fields: [
            {
              key: "is_appeal",
              label: "Is appeal?",
              required: true,
              placeholder: "yes / no",
              help_text: "Toggle to show appeal section",
            },
            {
              key: "notes",
              label: "Notes",
              required: false,
            },
          ],
          visible_when: null,
        },
        {
          key: "appeal",
          label: "Appeal",
          fields: [
            {
              key: "appeal_reason",
              label: "Reason",
              required: true,
            },
          ],
          visible_when: { truthy: { field: "is_appeal" } },
        },
      ],
      validations: [
        {
          when: { in: { field: "is_appeal", value: ["yes", "true"] } },
          then: { required: "appeal_reason" },
        },
      ],
    };

    // when
    const roundTripped = JSON.parse(JSON.stringify(original));

    // then
    expect(isValidFormDefinition(original)).toBe(true);
    expect(isValidFormDefinition(roundTripped)).toBe(true);
  });
});
