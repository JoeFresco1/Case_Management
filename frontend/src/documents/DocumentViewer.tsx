import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, isForbiddenLike } from "../api/client";
import { ContentPreview } from "./ContentPreview";
import { AnnotationLayer } from "./AnnotationLayer";
import { CheckoutIndicator } from "./CheckoutIndicator";
import { RevisionHistory } from "./RevisionHistory";
import { documentQueryKeys } from "./queryKeys";
import styles from "./Documents.module.css";

type ViewerMode = "paged" | "thumbnail" | "detail";

const MODES: ViewerMode[] = ["paged", "thumbnail", "detail"];

/**
 * Paged / thumbnail / detail document viewer (T5 viewer pipeline, C-8).
 *
 * - Paged: previous/next page stepping over the annotation page space.
 * - Thumbnail: a strip of page buttons with per-page annotation counts.
 * - Detail: the selected version's metadata, authorized download links,
 *   checkout state, revision history, and the annotation overlay layer.
 *
 * The page space is derived from annotation marks (max page, min 1):
 * the backend stores no page count, so the viewer grows the strip as
 * marks appear. Version selection pins the annotation layer to one
 * filed version ("drawn on v2, viewing v3" stays visible).
 *
 * Restricted/unknown documents render a metadata-free forbidden state:
 * no file name, version, hash, holder, or annotation content leaks.
 * Callers must not pass display metadata as children — the viewer only
 * renders server-authorized rows, never prop-supplied file names, when
 * the fetch fails.
 */
export function DocumentViewer({ documentId }: { documentId: string }) {
  const [mode, setMode] = useState<ViewerMode>("paged");
  const [page, setPage] = useState(1);
  const [versionFilter, setVersionFilter] = useState<number | undefined>(
    undefined,
  );

  const versionsQuery = useQuery({
    queryKey: documentQueryKeys.versions(documentId),
    queryFn: () => api.viewer.listVersions(documentId),
    staleTime: 15_000,
  });

  const annotationsQuery = useQuery({
    // why a dedicated "counts" key: the annotation layer below pages
    // through the same endpoint with limit/offset slices under the
    // `annotations` key. Sharing that key would let TanStack dedupe the
    // two observers into one fetch, truncating the page strip to the
    // layer's current slice. The counts query always reads up to 200
    // marks so the thumbnail strip covers every annotated page.
    queryKey: [
      "documents",
      documentId,
      "annotations",
      versionFilter ?? "all",
      "counts",
    ],
    queryFn: () =>
      api.viewer.listAnnotations(documentId, {
        document_version: versionFilter,
        limit: 200,
      }),
    staleTime: 10_000,
  });

  const forbidden =
    (versionsQuery.error && isForbiddenLike(versionsQuery.error)) ||
    (annotationsQuery.error && isForbiddenLike(annotationsQuery.error));

  const pageCount = useMemo(() => {
    const marks = annotationsQuery.data ?? [];
    const max = marks.reduce((acc, mark) => Math.max(acc, mark.page), 1);
    return Math.max(1, max);
  }, [annotationsQuery.data]);

  const countByPage = useMemo(() => {
    const counts = new Map<number, number>();
    for (const mark of annotationsQuery.data ?? []) {
      counts.set(mark.page, (counts.get(mark.page) ?? 0) + 1);
    }
    return counts;
  }, [annotationsQuery.data]);

  if (forbidden) {
    return (
      <section className={styles.viewer} aria-label="Document viewer">
        <p className={styles.empty} role="alert">
          Document is restricted or not available.
        </p>
      </section>
    );
  }

  const versions = versionsQuery.data ?? [];
  const current = versions.reduce<(typeof versions)[number] | undefined>(
    (latest, version) =>
      !latest || version.version_number > latest.version_number
        ? version
        : latest,
    undefined,
  );
  const selected =
    versions.find((version) => version.version_number === versionFilter) ??
    current;

  const clampPage = (next: number) =>
    setPage(Math.min(Math.max(1, next), pageCount));

  return (
    <section className={styles.viewer} aria-label="Document viewer">
      <header className={styles.viewerHeader}>
        <div>
          <h2>Document review</h2>
          {current && (
            <span className={styles.meta}>
              Current version {current.version_number} · {current.content_type}
            </span>
          )}
        </div>
      </header>
      <div className={styles.modeSwitch} role="group" aria-label="Viewer mode">
        {MODES.map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={mode === name}
            aria-label={name}
            onClick={() => setMode(name)}
          >
            {
              {
                paged: "Review",
                thumbnail: "Annotation pages",
                detail: "File history",
              }[name]
            }
          </button>
        ))}
      </div>

      {versionsQuery.isLoading ? (
        <p>Loading document…</p>
      ) : versionsQuery.error ? (
        <p className={styles.error} role="alert">
          Document could not be loaded.
        </p>
      ) : (
        <div className={styles.detail}>
          <label>
            Annotation version
            <select
              value={versionFilter === undefined ? "" : String(versionFilter)}
              onChange={(event) =>
                setVersionFilter(
                  event.target.value === ""
                    ? undefined
                    : Number(event.target.value),
                )
              }
              aria-label="Annotation version filter"
            >
              <option value="">All versions</option>
              {versions.map((version) => (
                <option
                  key={version.version_number}
                  value={version.version_number}
                >
                  v{version.version_number} — {version.sha256.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          <div className={styles.meta}>
            {selected && (
              <>
                Version {selected.version_number} ·{" "}
                {selected.size.toLocaleString()} bytes
                {selected.uploaded_at && (
                  <>
                    {" "}
                    · Filed{" "}
                    {new Date(selected.uploaded_at).toLocaleDateString()}
                  </>
                )}
              </>
            )}
          </div>
          <div className={styles.inlineRow}>
            <a href={api.viewer.contentUrl(documentId, "original")}>
              Download current original
            </a>
            <a href={api.viewer.contentUrl(documentId, "redacted")}>
              Download redacted copy
            </a>
          </div>
        </div>
      )}

      {mode === "paged" && current && (
        <ContentPreview
          key={documentId + current.version_number}
          documentId={documentId}
          size={current.size}
          type={current.content_type}
        />
      )}
      {(mode === "paged" || mode === "thumbnail") && (
        <div>
          <h3>Annotation pages</h3>
          <ul className={styles.pageStrip}>
            {Array.from({ length: pageCount }, (_, index) => index + 1).map(
              (pageNumber) => (
                <li key={pageNumber}>
                  <button
                    type="button"
                    className={styles.thumbnail}
                    aria-pressed={page === pageNumber}
                    aria-label={`Page ${pageNumber}, ${countByPage.get(pageNumber) ?? 0} annotations`}
                    onClick={() => setPage(pageNumber)}
                  >
                    p{pageNumber} · {countByPage.get(pageNumber) ?? 0}
                  </button>
                </li>
              ),
            )}
          </ul>
          {mode === "paged" && (
            <div className={styles.inlineRow}>
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => clampPage(page - 1)}
              >
                Previous page
              </button>
              <span className={styles.meta}>
                Page {page} of {pageCount}
              </span>
              <button
                type="button"
                disabled={page >= pageCount}
                onClick={() => clampPage(page + 1)}
              >
                Next page
              </button>
            </div>
          )}
        </div>
      )}

      {(mode === "paged" || mode === "detail") && (
        <AnnotationLayer
          documentId={documentId}
          page={page}
          documentVersion={versionFilter}
        />
      )}

      {mode === "detail" && (
        <>
          <CheckoutIndicator documentId={documentId} />
          <RevisionHistory documentId={documentId} />
        </>
      )}
    </section>
  );
}
