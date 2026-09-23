import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RedactedValue, SensitivityBadge, isRedactedMarker } from "./Sensitivity";

describe("SensitivityBadge", () => {
  it("renders the sensitivity level as a labeled chip", () => {
    render(<SensitivityBadge level="restricted" />);
    expect(screen.getByText("restricted")).toBeInTheDocument();
  });

  it("falls back gracefully for unknown levels", () => {
    render(<SensitivityBadge level="pickles" />);
    expect(screen.getByText("pickles")).toBeInTheDocument();
  });
});

describe("RedactedValue", () => {
  it("renders (restricted) with the reason as tooltip and never the content", () => {
    render(<RedactedValue value={{ redacted: true, reason: "sensitivity" }} />);
    expect(screen.getByText("(restricted)")).toBeInTheDocument();
    expect(screen.getByText("(restricted)")).toHaveAttribute("title", "sensitivity");
  });

  it("detects only exact redacted markers", () => {
    expect(isRedactedMarker({ redacted: true, reason: "x" })).toBe(true);
    expect(isRedactedMarker({ redacted: false })).toBe(false);
    expect(isRedactedMarker({ content: "secret" })).toBe(false);
    expect(isRedactedMarker(null)).toBe(false);
  });
});
