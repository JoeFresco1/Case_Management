import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ParticipantsPanel } from "./ParticipantsPanel";

describe("ParticipantsPanel", () => {
  it("renders participant names, roles, sources, and primary marker", () => {
    render(
      <ParticipantsPanel
        participants={[
          {
            id: "participant-1",
            display_name: "Synthetic requester",
            role: "requester",
            source: "intake",
            is_primary: true,
          },
        ]}
      />,
    );

    expect(screen.getByRole("heading", { name: "Participants" })).toBeInTheDocument();
    expect(screen.getByText("Synthetic requester")).toBeInTheDocument();
    expect(screen.getByText("requester · intake")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
  });

  it("shows an empty state when no participants are returned", () => {
    render(<ParticipantsPanel participants={[]} />);
    expect(screen.getByText("No participants attached.")).toBeInTheDocument();
  });
});
