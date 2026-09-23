/**
 * HoldBadge — render the per-case legal-hold state (P08-US1, US2 / FM-039).
 *
 * The component is intentionally decoupled from the API client: it
 * accepts a {@link LegalHoldSummary} value object so any backend that
 * emits the same wire shape (the OpenAPI document is the contract)
 * can plug in without code changes. When the case has no active hold
 * the badge renders a muted "No hold" marker so the workspace UI never
 * has to special-case the missing-row case.
 *
 * Tone mapping: red for active (prevention is the loud state the
 * operator must notice); muted gray for "no hold" so the badge
 * disappears from the operator's attention until something happens.
 */
export type HoldTone = "danger" | "muted";

export interface LegalHoldSummary {
  is_active: boolean;
  placed_by: string;
  placed_reason: string;
  placed_at: string;
  released_by: string | null;
  released_reason: string | null;
  released_at: string | null;
}

const TONE: Record<"active" | "inactive", HoldTone> = {
  active: "danger",
  inactive: "muted",
};

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function HoldBadge({
  hold,
}: {
  hold: LegalHoldSummary | null | undefined;
}) {
  if (!hold) {
    return (
      <span className="hold-badge hold-badge--muted" data-state="inactive">
        <span className="hold-badge__dot" aria-hidden="true" />
        No hold
      </span>
    );
  }

  const tone = TONE[hold.is_active ? "active" : "inactive"];
  const label = hold.is_active ? "Under legal hold" : "Hold released";
  const detail = hold.is_active
    ? `${hold.placed_reason} (placed ${formatTimestamp(hold.placed_at)})`
    : hold.released_reason && hold.released_at
      ? `released ${formatTimestamp(hold.released_at)}`
      : "released";
  return (
    <span
      className={`hold-badge hold-badge--${tone}`}
      data-state={hold.is_active ? "active" : "inactive"}
      title={detail}
    >
      <span className="hold-badge__dot" aria-hidden="true" />
      {label}
      <span className="hold-badge__detail"> · {detail}</span>
    </span>
  );
}

export default HoldBadge;