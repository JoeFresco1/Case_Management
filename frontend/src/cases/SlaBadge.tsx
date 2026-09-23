/**
 * SlaBadge — render the case-summary ``sla_state`` slot (P02-US6 / FM-018).
 *
 * The component is intentionally decoupled from the API client: it
 * accepts a {@link SlaStateView} value object so any backend that
 * emits the same wire shape (the OpenAPI document is the contract)
 * can plug in without code changes.
 *
 * Tiers (worst-first): breached > warning > active > paused >
 * completed > none. The colors and labels are the AGENTS.md §13
 * UX contract: red for breached, amber for warning, green for
 * active, gray for the rest.
 */
export type SlaStateName =
  | "breached"
  | "warning"
  | "active"
  | "paused"
  | "completed"
  | "none";

export interface SlaStateView {
  state: SlaStateName;
  next_due_at: string | null;
  breached_count: number;
  warning_count: number;
  active_count: number;
  paused_count: number;
  completed_count: number;
  clock_keys: readonly string[];
}

const TIER_LABEL: Record<SlaStateName, string> = {
  breached: "SLA breached",
  warning: "SLA warning",
  active: "SLA active",
  paused: "SLA paused",
  completed: "SLA completed",
  none: "No SLA",
};

const TIER_TONE: Record<SlaStateName, "danger" | "warning" | "ok" | "muted"> =
  {
    breached: "danger",
    warning: "warning",
    active: "ok",
    paused: "muted",
    completed: "muted",
    none: "muted",
  };

/**
 * Format an ISO timestamp as a short human-readable deadline.
 * Returns the empty string when the timestamp is null. The
 * formatter deliberately uses the runtime locale so the badge
 * reads naturally in the operator's UI.
 */
export function formatDeadline(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function SlaBadge({ state }: { state: SlaStateView | null | undefined }) {
  if (!state) {
    return <span className="sla-badge sla-badge--muted">No SLA</span>;
  }
  const tier = state.state;
  const label = TIER_LABEL[tier];
  const tone = TIER_TONE[tier];
  const deadline = formatDeadline(state.next_due_at);
  const keyHint =
    state.clock_keys.length > 0 ? state.clock_keys.join(", ") : "";
  return (
    <span
      className={`sla-badge sla-badge--${tone}`}
      data-state={tier}
      title={deadline ? `${label} — due ${deadline}` : label}
    >
      <span className="sla-badge__dot" aria-hidden="true" />
      {label}
      {deadline ? <span className="sla-badge__deadline"> · {deadline}</span> : null}
      {keyHint ? <span className="sla-badge__keys"> ({keyHint})</span> : null}
    </span>
  );
}

export default SlaBadge;
