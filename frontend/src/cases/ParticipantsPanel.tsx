import type { components } from "../api/types.gen";
import styles from "./CaseWorkspace.module.css";

type CaseSummary = components["schemas"]["CaseSummary"];

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

/** Displays the server-authorized participants attached to a case. */
export function ParticipantsPanel({ participants }: { participants: CaseSummary["participants"] }) {
  if (!participants || participants.length === 0) {
    return (
      <section aria-labelledby="participants-heading">
        <h2 id="participants-heading">Participants</h2>
        <p className={styles.empty}>No participants attached.</p>
      </section>
    );
  }

  return (
    <section aria-labelledby="participants-heading">
      <h2 id="participants-heading">Participants</h2>
      <ul className={styles.participantList}>
        {participants.map((participant, index) => {
          const record = asRecord(participant);
          const name = typeof record?.display_name === "string" ? record.display_name : "Unnamed participant";
          const role = typeof record?.role === "string" ? record.role : "Role unavailable";
          const source = typeof record?.source === "string" ? record.source : null;
          const primary = record?.is_primary === true;
          return (
            <li key={typeof record?.id === "string" ? record.id : index} className={styles.participantItem}>
              <strong>{name}</strong>
              {primary && <span className={styles.primaryBadge}>Primary</span>}
              <div className={styles.meta}>
                {role}{source ? ` · ${source}` : ""}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
