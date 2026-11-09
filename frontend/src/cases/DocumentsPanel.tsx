import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import { DocumentViewer } from "../documents/DocumentViewer";
import styles from "../documents/Documents.module.css";

type DocumentSearchHit = components["schemas"]["DocumentSearchHit"];

function isRedactedSnippet(
  value: DocumentSearchHit["snippet"],
): value is { redacted: true; reason?: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { redacted?: unknown }).redacted === true
  );
}

function DocumentPreview({
  snippet,
}: {
  snippet: DocumentSearchHit["snippet"];
}) {
  if (isRedactedSnippet(snippet)) {
    return <span className={styles.redacted}>Preview restricted</span>;
  }
  return typeof snippet === "string" && snippet.trim() ? (
    <span>{snippet}</span>
  ) : null;
}

export function DocumentsPanel({ caseId }: { caseId: string }) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [classification, setClassification] = useState("Correspondence");
  const [sensitivity, setSensitivity] = useState("internal");
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(
    null,
  );
  const [filter, setFilter] = useState({ query: "", classification: "" });
  const [offset, setOffset] = useState(0);
  const documentsQuery = useQuery({
    queryKey: ["documents", caseId, filter, offset],
    queryFn: () =>
      api.searchCaseDocuments(caseId, {
        query: filter.query || undefined,
        classification: filter.classification || undefined,
        limit: 50,
        offset,
      }),
  });
  const uploadMutation = useMutation({
    mutationFn: () => {
      if (!selectedFile) throw new Error("Choose a file first.");
      return api.uploadDocument(caseId, selectedFile, {
        classification,
        sensitivity,
      });
    },
    onSuccess: async () => {
      setSelectedFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await queryClient.invalidateQueries({ queryKey: ["documents", caseId] });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cases", caseId] }),
        queryClient.invalidateQueries({ queryKey: ["transitions", caseId] }),
        queryClient.invalidateQueries({ queryKey: ["timeline", caseId] }),
      ]);
    },
  });

  return (
    <section className={styles.attachments} aria-labelledby="documents-heading">
      <header className={styles.sectionHeading}>
        <h2 id="documents-heading">Documents</h2>
        {documentsQuery.data && (
          <span className={styles.number}>
            {documentsQuery.data.total_considered ??
              documentsQuery.data.items.length}
          </span>
        )}
      </header>
      <form
        className={styles.documentFilter}
        aria-label="Filter case documents"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          setFilter({
            query: String(data.get("query") ?? "").trim(),
            classification: String(data.get("classification") ?? "").trim(),
          });
          setOffset(0);
        }}
        onReset={() => {
          setFilter({ query: "", classification: "" });
          setOffset(0);
        }}
      >
        <input
          name="query"
          type="search"
          aria-label="Find document by filename"
          placeholder="Find by filename"
        />
        <input
          name="classification"
          aria-label="Filter by classification"
          placeholder="Any classification"
        />
        <button type="submit">Filter</button>
        {(filter.query || filter.classification) && (
          <button type="reset">Clear</button>
        )}
      </form>
      {documentsQuery.isLoading && <p>Loading documents…</p>}
      {documentsQuery.error && (
        <p className={styles.error}>Documents could not be loaded.</p>
      )}
      {!documentsQuery.isLoading && !documentsQuery.error && (
        <ul className={styles.documentList}>
          {(documentsQuery.data?.items ?? []).map((document) => (
            <li key={document.id} className={styles.documentItem}>
              <div className={styles.docTitle}>
                <span className={styles.fileType} aria-hidden="true">
                  {document.file_name.split(".").length > 1
                    ? document.file_name
                        .split(".")
                        .pop()
                        ?.slice(0, 5)
                        .toUpperCase()
                    : "FILE"}
                </span>
                <div>
                  <strong>{document.file_name}</strong>
                  <div className={styles.meta}>
                    Filed {new Date(document.uploaded_at).toLocaleDateString()}
                  </div>
                </div>
              </div>
              <div className={styles.meta}>
                {document.classification} · {document.sensitivity} · v
                {document.current_version}
              </div>
              {document.snippet && (
                <p className={styles.documentPreview}>
                  <DocumentPreview snippet={document.snippet} />
                </p>
              )}
              <button
                type="button"
                aria-expanded={selectedDocumentId === document.id}
                onClick={() =>
                  setSelectedDocumentId((prev) =>
                    prev === document.id ? null : document.id,
                  )
                }
              >
                {selectedDocumentId === document.id
                  ? "Close viewer"
                  : "Review here"}
              </button>
              <a
                className={styles.fullReview}
                href={`/documents/${document.id}?caseId=${encodeURIComponent(caseId)}`}
              >
                Open full viewer →
              </a>
              {selectedDocumentId === document.id && (
                <DocumentViewer documentId={document.id} />
              )}
            </li>
          ))}
          {(documentsQuery.data?.items ?? []).length === 0 && (
            <li className={styles.empty}>
              {filter.query || filter.classification
                ? "No matching documents."
                : "No documents attached."}
            </li>
          )}
        </ul>
      )}
      {(offset > 0 || documentsQuery.data?.next_cursor) && (
        <div className={styles.inlineRow}>
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 50))}
          >
            Previous documents
          </button>
          <button
            type="button"
            disabled={!documentsQuery.data?.next_cursor}
            onClick={() => setOffset(offset + 50)}
          >
            Next documents
          </button>
        </div>
      )}
      <h3 className={styles.uploadTitle}>Attach a file</h3>
      <form
        className={styles.documentUpload}
        onSubmit={(event) => {
          event.preventDefault();
          uploadMutation.mutate();
        }}
      >
        <label>
          File
          <input
            ref={fileInput}
            type="file"
            onChange={(event) =>
              setSelectedFile(event.target.files?.[0] ?? null)
            }
          />
        </label>
        <label>
          Classification
          <input
            value={classification}
            onChange={(event) => setClassification(event.target.value)}
            required
          />
        </label>
        <label>
          Sensitivity
          <select
            value={sensitivity}
            onChange={(event) => setSensitivity(event.target.value)}
          >
            <option value="public">Public</option>
            <option value="internal">Internal</option>
            <option value="confidential">Confidential</option>
            <option value="restricted">Restricted</option>
            <option value="phi">PHI</option>
          </select>
        </label>
        <button
          type="submit"
          disabled={!selectedFile || uploadMutation.isPending}
        >
          {uploadMutation.isPending ? "Uploading…" : "Upload document"}
        </button>
      </form>
      {uploadMutation.isSuccess && !selectedFile && (
        <p className={styles.meta} role="status">
          Document uploaded.
        </p>
      )}
      {uploadMutation.error && (
        <p className={styles.error} role="alert">
          {uploadMutation.error instanceof ApiClientError
            ? uploadMutation.error.message
            : "Document upload failed."}
        </p>
      )}
    </section>
  );
}
