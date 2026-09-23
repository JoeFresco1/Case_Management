import styles from "./CaseWorkspace.module.css";

/**
 * Display the rename history of a field's label (P05-US8 / FM-012).
 *
 * The component is intentionally decoupled from any HTTP shape: it
 * accepts a list of {@link LabelRename} entries so any backend that
 * surfaces the rename history can plug in without code changes.
 *
 * Ordering: entries are rendered in the order received. The domain
 * guarantees ascending ``at_version``; callers should not sort here.
 */
export interface LabelRename {
  previous: string;
  current: string;
  at_version: number;
  at?: string | null;
}

export function LabelHistory({
  fieldKey,
  history,
  currentLabel,
}: {
  fieldKey: string;
  history: readonly LabelRename[];
  currentLabel: string;
}) {
  if (history.length === 0) {
    return (
      <p className={styles.empty}>
        Field <code>{fieldKey}</code> label has never been renamed.
      </p>
    );
  }
  return (
    <div className={styles.history}>
      <h3>
        Label history for <code>{fieldKey}</code>
      </h3>
      <p>
        Current label: <strong>{currentLabel}</strong>
      </p>
      <ol>
        {history.map((entry, idx) => (
          <li key={`${entry.at_version}-${idx}`}>
            v{entry.at_version}: <em>{entry.previous || "(initial)"}</em>{" "}
            &rarr; <strong>{entry.current}</strong>
            {entry.at ? <span className={styles.meta}> ({entry.at})</span> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * Synthetic preview used until the admin schema endpoint ships the
 * rename history. Marked SYNTHETIC so reviewers do not mistake it for
 * real data; the real history comes from the published case type
 * version once FM-008 admin surfaces expose it.
 */
export const SYNTHETIC_LABEL_HISTORY: readonly LabelRename[] = [
  {
    previous: "",
    current: "Title",
    at_version: 1,
    at: "2026-01-15T00:00:00Z",
  },
  {
    previous: "Title",
    current: "Case Title",
    at_version: 2,
    at: "2026-08-31T00:00:00Z",
  },
];