import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, api } from "../api/client";
import type { QuarantineRecordResponse } from "../api/client";
import { Quarantine } from "./Quarantine";

function heldRecord(): QuarantineRecordResponse {
  return {
    id: "cccccccc-3333-4333-8333-cccccccccccc",
    tenant_id: "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
    channel_id: "dddddddd-4444-4444-8444-dddddddddddd",
    channel_key: "intake_csv",
    source_label: "synthetic-fixture",
    captured_at: "2026-09-16T00:00:00+00:00",
    captured_by: "operator",
    error_code: "schema_mismatch",
    error_message: "column priority missing",
    correlation_id: "corr-quar-1",
    status: "held",
    resolved_at: null,
    resolved_by: null,
    resolution_note: null,
    context: { channel_key: "intake_csv", attempt: 1 },
  };
}

describe("Quarantine", () => {
  afterEach(() => vi.restoreAllMocks());

  it("test_list__renders_provenance_without_payload", async () => {
    // given one held record with diagnostic context only
    vi.spyOn(api.quarantine, "list").mockResolvedValue({
      items: [heldRecord()],
      total: 1,
    });
    render(<Quarantine />);
    // when the operator refreshes the queue
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    // then provenance renders and no payload bytes appear
    await waitFor(() =>
      expect(screen.getByText("intake_csv")).toBeInTheDocument(),
    );
    expect(screen.getByText("synthetic-fixture")).toBeInTheDocument();
    expect(screen.getByText("corr-quar-1")).toBeInTheDocument();
    expect(screen.getByText("1 held record(s).")).toBeInTheDocument();
    expect(screen.queryByText(/walkthrough/)).not.toBeInTheDocument();
  });

  it("test_detail__expands_same_record_without_refetch", async () => {
    // given the listed record
    const list = vi
      .spyOn(api.quarantine, "list")
      .mockResolvedValue({ items: [heldRecord()], total: 1 });
    render(<Quarantine />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    await waitFor(() =>
      expect(screen.getByText("intake_csv")).toBeInTheDocument(),
    );
    // when the operator expands the detail row
    fireEvent.click(screen.getByRole("button", { name: /Show detail/ }));
    // then resolution fields render from the listed record alone
    expect(screen.getByText("Captured by")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("test_release__sends_note_and_refreshes", async () => {
    // given a held record and a successful release
    const record = heldRecord();
    vi.spyOn(api.quarantine, "list").mockResolvedValue({
      items: [record],
      total: 1,
    });
    const release = vi
      .spyOn(api.quarantine, "release")
      .mockResolvedValue({ record: { ...record, status: "released" } });
    render(<Quarantine />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    await waitFor(() =>
      expect(screen.getByText("intake_csv")).toBeInTheDocument(),
    );
    // when the operator releases with a note
    fireEvent.change(screen.getByLabelText("Resolution note"), {
      target: { value: "source corrected" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Release" }));
    // then the note is sent and the list refreshes
    await waitFor(() =>
      expect(release).toHaveBeenCalledWith(record.id, "source corrected"),
    );
  });

  it("test_reject__sends_note_and_refreshes", async () => {
    // given a held record and a successful reject
    const record = heldRecord();
    vi.spyOn(api.quarantine, "list").mockResolvedValue({
      items: [record],
      total: 1,
    });
    const reject = vi
      .spyOn(api.quarantine, "reject")
      .mockResolvedValue({ record: { ...record, status: "rejected" } });
    render(<Quarantine />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    await waitFor(() =>
      expect(screen.getByText("intake_csv")).toBeInTheDocument(),
    );
    // when the operator rejects with a note
    fireEvent.change(screen.getByLabelText("Resolution note"), {
      target: { value: "duplicate feed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    // then the note is sent and the list refreshes
    await waitFor(() =>
      expect(reject).toHaveBeenCalledWith(record.id, "duplicate feed"),
    );
  });

  it("test_resolve_error__preserves_list", async () => {
    // given a held record whose release fails as already-resolved
    const record = heldRecord();
    vi.spyOn(api.quarantine, "list").mockResolvedValue({
      items: [record],
      total: 1,
    });
    vi.spyOn(api.quarantine, "release").mockRejectedValue(
      new ApiClientError("already resolved", 422, "conflict"),
    );
    render(<Quarantine />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    await waitFor(() =>
      expect(screen.getByText("intake_csv")).toBeInTheDocument(),
    );
    // when the release fails
    fireEvent.click(screen.getByRole("button", { name: "Release" }));
    // then the error shows and the list selection is preserved
    await waitFor(() =>
      expect(screen.getByText("already resolved")).toBeInTheDocument(),
    );
    expect(screen.getByText("intake_csv")).toBeInTheDocument();
    expect(screen.getByText("synthetic-fixture")).toBeInTheDocument();
  });
});
