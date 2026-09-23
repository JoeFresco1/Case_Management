import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiClientError } from "../api/client";
import type { components } from "../api/types.gen";
import { DocumentViewer } from "../documents/DocumentViewer";
import styles from "./CaseWorkspace.module.css";

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

function DocumentPreview({ snippet }: { snippet: DocumentSearchHit["snippet"] }) {
  if (isRedactedSnippet(snippet)) {
    return <span className={styles.redacted}>Preview restricted</span>;
  }
  return <span>{typeof snippet === "string" ? snippet || "No preview available" : "No preview available"}</span>;
}

export function DocumentsPanel({ caseId }: { caseId: string }) {
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [classification, setClassification] = useState("Correspondence");
  const [sensitivity, setSensitivity] = useState("internal");
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const documentsQuery = useQuery({
    queryKey: ["documents", caseId],
    queryFn: () => api.searchCaseDocuments(caseId, { limit: 50 }),
  });
  const uploadMutation = useMutation({
    mutationFn: () => {
      if (!selectedFile) throw new Error("Choose a file first.");
      return api.uploadDocument(caseId, selectedFile, { classification, sensitivity });
    },
    onSuccess: async () => {
      setSelectedFile(null);
      await queryClient.invalidateQueries({ queryKey: ["documents", caseId] });
      await queryClient.invalidateQueries({ queryKey: ["cases", caseId] });
    },
  });

  return (
    <section aria-labelledby="documents-heading">
      <h2 id="documents-heading">Documents</h2>
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
            type="file"
            onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
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
          <select value={sensitivity} onChange={(event) => setSensitivity(event.target.value)}>
            <option value="public">Public</option>
            <option value="internal">Internal</option>
            <option value="confidential">Confidential</option>
            <option value="restricted">Restricted</option>
            <option value="phi">PHI</option>
          </select>
        </label>
        <button type="submit" disabled={!selectedFile || uploadMutation.isPending}>
          {uploadMutation.isPending ? "Uploading…" : "Upload document"}
        </button>
      </form>
      {uploadMutation.error && (
        <p className={styles.error} role="alert">
          {uploadMutation.error instanceof ApiClientError
            ? uploadMutation.error.message
            : "Document upload failed."}
        </p>
      )}
      {documentsQuery.isLoading && <p>Loading documents…</p>}
      {documentsQuery.error && <p className={styles.error}>Documents could not be loaded.</p>}
      {!documentsQuery.isLoading && !documentsQuery.error && (
        <ul className={styles.documentList}>
          {(documentsQuery.data?.items ?? []).map((document) => (
            <li key={document.id} className={styles.documentItem}>
              <strong>{document.file_name}</strong>
              <div className={styles.meta}>
                {document.classification} · {document.sensitivity} · v{document.current_version}
              </div>
              <p className={styles.documentPreview}>
                <DocumentPreview snippet={document.snippet} />
              </p>
              <button
                type="button"
                aria-expanded={selectedDocumentId === document.id}
                onClick={() =>
                  setSelectedDocumentId((prev) => (prev === document.id ? null : document.id))
                }
              >
                {selectedDocumentId === document.id ? "Close viewer" : "Open viewer"}
              </button>
              {selectedDocumentId === document.id && (
                <DocumentViewer documentId={document.id} />
              )}
            </li>
          ))}
          {(documentsQuery.data?.items ?? []).length === 0 && (
            <li className={styles.empty}>No documents attached.</li>
          )}
        </ul>
      )}
    </section>
  );
}
