import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, api, type ImportJob } from "../api/client";
import { ImportPreview } from "./ImportPreview";

function previewJob(): ImportJob {
  return {
    id: "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
    tenant_id: "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
    source_label: "synthetic-fixture",
    source_format: "csv",
    source_row_count: 3,
    status: "previewed",
    mapping: { version: 1, case_type_key: "ServiceRequest" },
    results: [
      {
        row_index: 1,
        outcome: "accepted",
        case_id: null,
        source_id: "SR-0001",
        error_code: null,
        error_message: null,
      },
      {
        row_index: 2,
        outcome: "accepted",
        case_id: null,
        source_id: "SR-0002",
        error_code: null,
        error_message: null,
      },
      {
        row_index: 3,
        outcome: "rejected",
        case_id: null,
        source_id: "SR-0003",
        error_code: "validation_failed",
        error_message: "priority not in allowed set",
      },
    ],
    reconciliation: {
      committed_count: 2,
      rejected_count: 1,
      skipped_count: 0,
    },
    created_at: "2026-09-16T00:00:00+00:00",
    committed_at: null,
    failure_reason: null,
    correlation_id: "corr-preview-1",
  };
}

describe("ImportPreview", () => {
  afterEach(() => vi.restoreAllMocks());

  it("test_dry_run_preview_renders_reconciliation__without_clearing_rows", async () => {
    // given a dry-run that accepts two rows and rejects one
    vi.spyOn(api.imports, "dryRun").mockResolvedValue({ job: previewJob() });
    render(<ImportPreview />);
    // when the operator runs the preview
    fireEvent.click(screen.getByRole("button", { name: "Dry-run" }));
    // then the reconciliation counts render and the entered rows stay
    await waitFor(() =>
      expect(screen.getByText(/Reconciliation/)).toBeInTheDocument(),
    );
    expect(screen.getByText("SR-0003")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Row 1 title") as HTMLInputElement,
    ).toHaveValue("Onboarding help");
    expect(
      screen.getByLabelText("Row 3 priority") as HTMLInputElement,
    ).toHaveValue("yesterday");
  });

  it("test_dry_run_validation_error__preserves_entered_rows", async () => {
    // given a dry-run that fails validation
    vi.spyOn(api.imports, "dryRun").mockRejectedValue(
      new ApiClientError("validation_failed", 422, "validation_failed", {
        field: "rows",
      }),
    );
    render(<ImportPreview />);
    // when the operator edits a row and runs the preview
    fireEvent.change(screen.getByLabelText("Row 1 title"), {
      target: { value: "Edited title" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Dry-run" }));
    // then the error shows and the edited value is preserved
    await waitFor(() =>
      expect(screen.getByText(/validation_failed/)).toBeInTheDocument(),
    );
    expect(
      screen.getByLabelText("Row 1 title") as HTMLInputElement,
    ).toHaveValue("Edited title");
  });

  it("test_commit__shows_created_cases_and_provenance_without_payload_values", async () => {
    // given a preview followed by a successful commit
    const job = previewJob();
    const committedJob: ImportJob = {
      ...job,
      status: "committed",
      correlation_id: "corr-commit-1",
      results: job.results.map((row) =>
        row.outcome === "accepted"
          ? {
              ...row,
              case_id: `case-${row.row_index}-id-0000-4000-8000-000000000000`,
            }
          : row,
      ),
    };
    vi.spyOn(api.imports, "dryRun").mockResolvedValue({ job });
    vi.spyOn(api.imports, "commit").mockResolvedValue({
      job: committedJob,
      created_case_ids: [
        "case-1-id-0000-4000-8000-000000000000",
        "case-2-id-0000-4000-8000-000000000000",
      ],
    });
    render(<ImportPreview />);
    fireEvent.click(screen.getByRole("button", { name: "Dry-run" }));
    await waitFor(() =>
      expect(screen.getByText(/Reconciliation/)).toBeInTheDocument(),
    );
    // when the operator commits the accepted rows
    fireEvent.click(
      screen.getByRole("button", { name: "Commit accepted rows" }),
    );
    // then created ids + provenance render without row payload bytes
    await waitFor(() =>
      expect(screen.getByText(/created/)).toBeInTheDocument(),
    );
    expect(
      screen.getByText("case-1-id-0000-4000-8000-000000000000", {
        exact: false,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Provenance/)).toBeInTheDocument();
    expect(screen.getByText("corr-commit-1")).toBeInTheDocument();
    const article = screen.getByText(/Provenance/).closest("p");
    expect(article?.textContent).not.toContain("Need a walkthrough");
  });
});
