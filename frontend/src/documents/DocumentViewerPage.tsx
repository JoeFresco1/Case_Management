import { useParams } from "react-router-dom";

import { DocumentViewer } from "./DocumentViewer";

/**
 * Standalone document-viewer route (C-8 / T5 viewer pipeline).
 *
 * Deep-linkable viewer for a single document: paged/thumbnail/detail
 * modes, annotation overlays, revision history, and checkout state.
 * Unknown ids render the viewer's own metadata-free forbidden state.
 */
export function DocumentViewerPage() {
  const params = useParams<{ documentId?: string }>();
  const documentId = params.documentId;
  if (!documentId) {
    return <p>Missing document id.</p>;
  }
  return (
    <article>
      <h1>Document viewer</h1>
      <DocumentViewer documentId={documentId} />
    </article>
  );
}
