/** Render an explicitly requested preview from the authorized current-content endpoint. */
import { useEffect, useState } from "react";

import { api } from "../api/client";
import styles from "./Documents.module.css";

const MAX_PREVIEW_BYTES = 8 * 1024 * 1024;
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

type Preview =
  | { kind: "loading" | "unavailable" }
  | { kind: "text" | "pdf" | "image"; value: string };

/** Load safe file types on demand and discard the bytes when the preview closes. */
export function ContentPreview({
  documentId,
  size,
  type,
}: {
  documentId: string;
  size: number;
  type: string;
}) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<Preview>({ kind: "loading" });
  const supported =
    type === "application/octet-stream" ||
    type.startsWith("text/") ||
    type === "application/pdf" ||
    IMAGE_TYPES.includes(type);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;

    async function load() {
      try {
        const response = await fetch(
          api.viewer.contentUrl(documentId, "original"),
          {
            signal: controller.signal,
            cache: "no-store",
            headers: { "X-Correlation-Id": crypto.randomUUID() },
          },
        );
        if (!response.ok) throw new Error("content unavailable");
        const blob = await response.blob();
        if (blob.size > MAX_PREVIEW_BYTES) throw new Error("preview too large");
        const mime = blob.type.split(";")[0].toLowerCase();
        const generic = !mime || mime === "application/octet-stream";
        // Office exports often lose their MIME label during upload. Only hand
        // bytes to the PDF renderer after checking the actual PDF signature.
        const isPdf =
          (generic || mime === "application/pdf") &&
          (await blob.slice(0, 5).text()) === "%PDF-";
        if (isPdf || IMAGE_TYPES.includes(mime)) {
          if (controller.signal.aborted) return;
          objectUrl = URL.createObjectURL(
            isPdf ? blob.slice(0, blob.size, "application/pdf") : blob,
          );
          setPreview({ kind: isPdf ? "pdf" : "image", value: objectUrl });
        } else if (mime.startsWith("text/") || generic) {
          const value = new TextDecoder("utf-8", { fatal: generic }).decode(
            await blob.arrayBuffer(),
          );
          if (
            generic &&
            Array.from(value.slice(0, 4096)).some(
              (char) => char < " " && !["\n", "\r", "\t"].includes(char),
            )
          ) {
            throw new Error("unsupported binary");
          }
          if (!controller.signal.aborted) setPreview({ kind: "text", value });
        } else {
          throw new Error("unsupported content");
        }
      } catch {
        if (!controller.signal.aborted) setPreview({ kind: "unavailable" });
      }
    }
    void load();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [open, documentId]);

  return (
    <div className={styles.preview}>
      <div className={styles.previewHeading}>
        <h3>File preview</h3>
        {supported && size <= MAX_PREVIEW_BYTES && (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => {
              setPreview({ kind: "loading" });
              setOpen(!open);
            }}
          >
            {open ? "Close preview" : "Preview current original"}
          </button>
        )}
      </div>
      {!supported || size > MAX_PREVIEW_BYTES ? (
        <p className={styles.meta}>Download this file to view its contents.</p>
      ) : null}
      {open && preview.kind === "loading" && <p role="status">Loading file…</p>}
      {open && preview.kind === "unavailable" && (
        <p role="alert">Preview unavailable for this file or identity.</p>
      )}
      {open && preview.kind === "text" && (
        <pre className={styles.paper}>{preview.value}</pre>
      )}
      {open && preview.kind === "image" && (
        <img
          className={styles.image}
          src={preview.value}
          alt="Current document content"
        />
      )}
      {open && preview.kind === "pdf" && (
        <object
          className={styles.pdf}
          type="application/pdf"
          data={preview.value}
          aria-label="Current document PDF"
        >
          <p>Use Download current original to open this PDF in your reader.</p>
        </object>
      )}
    </div>
  );
}
