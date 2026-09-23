import styles from "./Sensitivity.module.css";

/**
 * Sensitivity indicator + redacted-value renderer (P01-US4 / FM-011).
 *
 * The badge is a display indicator only: it never gates access.
 * Server-side redaction is the authoritative mechanism — the client
 * renders `{ redacted: true, reason }` markers as "(restricted)" and
 * never sees the underlying content.
 */

export type SensitivityLevel =
  | "public"
  | "internal"
  | "confidential"
  | "restricted"
  | "phi";

export interface RedactedMarker {
  redacted: true;
  reason: string;
}

export function isRedactedMarker(value: unknown): value is RedactedMarker {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { redacted?: unknown }).redacted === true
  );
}

const LEVEL_CLASS: Record<string, string> = {
  public: styles.levelPublic,
  internal: styles.levelInternal,
  confidential: styles.levelConfidential,
  restricted: styles.levelRestricted,
  phi: styles.levelPhi,
};

/** Case/field sensitivity chip. Display only — never an access gate. */
export function SensitivityBadge({ level }: { level: string }) {
  const cls = LEVEL_CLASS[level] ?? styles.levelUnknown;
  return (
    <span
      className={`${styles.badge} ${cls}`}
      title="Sensitivity indicator. Server-side redaction is authoritative; this badge never gates access."
    >
      {level}
    </span>
  );
}

/** Renders a redacted wire marker without ever exposing content. */
export function RedactedValue({ value }: { value: RedactedMarker }) {
  return (
    <span className={styles.redacted} title={value.reason}>
      (restricted)
    </span>
  );
}
