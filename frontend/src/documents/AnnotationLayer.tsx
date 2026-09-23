import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError, api, isForbiddenLike } from "../api/client";
import type { AnnotationResponse } from "../api/client";
import { documentQueryKeys } from "./queryKeys";
import styles from "./Documents.module.css";

const ANNOTATION_KINDS = ["highlight", "text_box", "stamp", "freehand"] as const;
const PAGE_SIZE = 20;

function errorMessage(error: unknown): string {
  if (error instanceof ApiClientError) return error.message;
  return "Annotation operation failed.";
}

function AnnotationRow({
  documentId,
  annotation,
}: {
  documentId: string;
  annotation: AnnotationResponse;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(annotation.content ?? "");
  const [error, setError] = useState<string | null>(null);

  const invalidate = () =>
    void queryClient.invalidateQueries({
      queryKey: ["documents", documentId, "annotations"],
    });

  const save = useMutation({
    mutationFn: () =>
      api.viewer.updateAnnotation(documentId, annotation.id, {
        content,
        clear_content: false,
      }),
    onSuccess: () => {
      setEditing(false);
      setError(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const remove = useMutation({
    mutationFn: () => api.viewer.deleteAnnotation(documentId, annotation.id),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  return (
    <li className={styles.item}>
      <span className={styles.badge}>{annotation.kind}</span>
      <span className={styles.badge}>p{annotation.page}</span>
      <span className={styles.badge}>v{annotation.document_version}</span>
      {editing ? (
        <div className={styles.inlineRow}>
          <label>
            Caption
            <input
              value={content}
              onChange={(event) => setContent(event.target.value)}
              aria-label={`Caption for annotation ${annotation.id}`}
            />
          </label>
          <button type="button" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button type="button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </div>
      ) : (
        <div>
          <span>{annotation.content ?? <em>(no caption)</em>}</span>{" "}
          <button type="button" onClick={() => setEditing(true)}>
            Edit
          </button>{" "}
          <button
            type="button"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

/**
 * Annotation overlay layer UI (T5 viewer pipeline).
 *
 * Annotations are markup pinned to `(document_version, page)` and never
 * mutate document bytes. The form builds the page-relative geometry
 * mapping (`x/y/w/h` in 0..1 plus an optional color hint); `text_box`
 * requires a caption, other kinds treat it as optional.
 *
 * Forbidden/unknown documents render a metadata-free message: no
 * annotation content, geometry, or author leaks.
 */
export function AnnotationLayer({
  documentId,
  page,
  documentVersion,
}: {
  documentId: string;
  page: number;
  documentVersion?: number;
}) {
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [kind, setKind] = useState<(typeof ANNOTATION_KINDS)[number]>("highlight");
  const [content, setContent] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: documentQueryKeys.annotations(documentId, documentVersion, offset),
    queryFn: () =>
      api.viewer.listAnnotations(documentId, {
        document_version: documentVersion,
        limit: PAGE_SIZE,
        offset,
      }),
    staleTime: 10_000,
  });

  const create = useMutation({
    mutationFn: () => {
      if (kind === "text_box" && !content.trim()) {
        throw new Error("Text-box annotations require a caption.");
      }
      return api.viewer.createAnnotation(documentId, {
        document_version: documentVersion ?? 1,
        page,
        kind,
        geometry: { x: 0.1, y: 0.2, w: 0.3, h: 0.05 },
        content: content.trim() ? content.trim() : null,
      });
    },
    onSuccess: () => {
      setContent("");
      setFormError(null);
      void queryClient.invalidateQueries({
        queryKey: ["documents", documentId, "annotations"],
      });
    },
    onError: (e: unknown) =>
      setFormError(e instanceof Error ? e.message : errorMessage(e)),
  });

  if (listQuery.isLoading) return <p>Loading annotations…</p>;
  if (listQuery.error) {
    if (isForbiddenLike(listQuery.error)) {
      return (
        <p className={styles.empty} role="alert">
          Annotations are restricted or not available.
        </p>
      );
    }
    return (
      <p className={styles.error} role="alert">
        Annotations could not be loaded.
      </p>
    );
  }

  const marks = (listQuery.data ?? []).filter((mark) => mark.page === page);

  return (
    <section aria-label="Annotation layer">
      <h3>Annotations — page {page}</h3>
      {marks.length === 0 ? (
        <p className={styles.empty}>No annotations on this page.</p>
      ) : (
        <ul className={styles.list}>
          {marks.map((mark) => (
            <AnnotationRow key={mark.id} documentId={documentId} annotation={mark} />
          ))}
        </ul>
      )}
      <div className={styles.inlineRow}>
        <button
          type="button"
          disabled={offset === 0}
          onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
        >
          Previous
        </button>
        <button
          type="button"
          disabled={(listQuery.data ?? []).length < PAGE_SIZE}
          onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <h4>Add annotation</h4>
        <label>
          Kind
          <select
            value={kind}
            onChange={(event) =>
              setKind(event.target.value as (typeof ANNOTATION_KINDS)[number])
            }
          >
            {ANNOTATION_KINDS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Caption {kind === "text_box" ? "(required)" : "(optional)"}
          <input
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder={kind === "text_box" ? "Annotation text" : "Optional caption"}
          />
        </label>
        <button type="submit" disabled={create.isPending}>
          {create.isPending ? "Adding…" : "Add annotation"}
        </button>
        {formError && (
          <p className={styles.error} role="alert">
            {formError}
          </p>
        )}
      </form>
    </section>
  );
}
