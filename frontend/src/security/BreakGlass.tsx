import { useEffect, useMemo, useState } from "react";
import styles from "./BreakGlass.module.css";

/**
 * Break-glass console (P11-US10 / FM-035 / P06-US9).
 *
 * The component is intentionally framework-light: it accepts a
 * {@link BreakGlassApi} so the host application can wire it to the
 * generated OpenAPI client, a manual fetch, or a Storybook stub
 * without code changes. The "prominently flagged" requirement from
 * the pillar is enforced visually: while the grant is live the
 * timer badge pulses; on expiry the badge collapses to a red "EXPIRED"
 * chip; a denied action surfaces the structured denial reason inline.
 *
 * Cross-tenant search (P06-US9) is the primary scope in this slice;
 * the UI surfaces the scope selector so an administrator can mint a
 * different override without leaving the screen.
 */

export type BreakGlassScope = "cross_tenant_search" | "cross_tenant_case_read";

export interface BreakGlassGrant {
  grant_id: string;
  tenant_id: string;
  target_user_id: string;
  scope: BreakGlassScope;
  reason: string;
  granted_by: string;
  granted_at: string;
  expires_at: string;
  revoked_at?: string | null;
  revoked_by?: string | null;
}

export interface BreakGlassApi {
  create(input: {
    target_user_id: string;
    scope: BreakGlassScope;
    reason: string;
    duration_minutes: number;
  }): Promise<BreakGlassGrant>;
  revoke(grant_id: string): Promise<BreakGlassGrant>;
}

const DEFAULT_DURATION_MINUTES = 15;
const SCOPE_LABELS: Record<BreakGlassScope, string> = {
  cross_tenant_search: "Cross-tenant case search (auditor)",
  cross_tenant_case_read: "Cross-tenant case read",
};

function formatRemaining(ms: number): string {
  if (ms <= 0) return "expired";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}

/**
 * Single live-grant badge with a countdown timer. Renders the reason
 * inline so the auditor always sees the justification behind the
 * override — the "prominently flagged" requirement.
 */
export function BreakGlassTimerBadge({ grant }: { grant: BreakGlassGrant }) {
  const expiry = useMemo(() => new Date(grant.expires_at).getTime(), [grant.expires_at]);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const remaining = expiry - now;
  const expired = remaining <= 0;
  const revoked = grant.revoked_at !== null && grant.revoked_at !== undefined;

  return (
    <div
      className={`${styles.badge} ${expired || revoked ? styles.badgeInactive : styles.badgeActive}`}
      role="status"
      aria-live="polite"
    >
      <strong>{revoked ? "REVOKED" : expired ? "EXPIRED" : "BREAK-GLASS ACTIVE"}</strong>
      <span className={styles.timer}>{formatRemaining(remaining)}</span>
      <span className={styles.scope}>{SCOPE_LABELS[grant.scope]}</span>
      <span className={styles.reason} title={grant.reason}>
        Reason: {grant.reason}
      </span>
    </div>
  );
}

export interface BreakGlassConsoleProps {
  api: BreakGlassApi;
  /** The current actor's id — pre-filled as the grant target. */
  selfUserId: string;
}

/**
 * The grant-revocation console: form to mint a new override and a
 * list of live badges for the grants the actor currently holds. The
 * console is the only UI path that calls the HTTP router; the
 * search and case-read paths consult the server-side check directly.
 */
export function BreakGlassConsole({ api, selfUserId }: BreakGlassConsoleProps) {
  const [scope, setScope] = useState<BreakGlassScope>("cross_tenant_search");
  const [reason, setReason] = useState("");
  const [durationMinutes, setDurationMinutes] = useState(DEFAULT_DURATION_MINUTES);
  const [targetUserId, setTargetUserId] = useState(selfUserId);
  const [grants, setGrants] = useState<BreakGlassGrant[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (reason.trim().length === 0) {
      setError("Reason is required.");
      return;
    }
    setSubmitting(true);
    try {
      const grant = await api.create({
        target_user_id: targetUserId,
        scope,
        reason: reason.trim(),
        duration_minutes: durationMinutes,
      });
      setGrants((current) => [grant, ...current]);
      setReason("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to grant break-glass access.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRevoke(grantId: string) {
    setError(null);
    try {
      const revoked = await api.revoke(grantId);
      setGrants((current) =>
        current.map((g) => (g.grant_id === grantId ? revoked : g))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke break-glass grant.");
    }
  }

  return (
    <section className={styles.console}>
      <header className={styles.header}>
        <h2>Break-glass override</h2>
        <p>
          Time-bounded, reason-bearing exceptional access. Every action performed
          under the override is audit-flagged. Maximum window: 24h.
        </p>
      </header>
      <form className={styles.form} onSubmit={handleSubmit}>
        <label>
          Target user
          <input
            value={targetUserId}
            onChange={(e) => setTargetUserId(e.target.value)}
            required
          />
        </label>
        <label>
          Scope
          <select value={scope} onChange={(e) => setScope(e.target.value as BreakGlassScope)}>
            {(Object.keys(SCOPE_LABELS) as BreakGlassScope[]).map((value) => (
              <option key={value} value={value}>
                {SCOPE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Reason
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            minLength={1}
            maxLength={1024}
            required
            placeholder="Explain the emergency and why the override is required."
          />
        </label>
        <label>
          Duration (minutes)
          <input
            type="number"
            min={1}
            max={24 * 60}
            value={durationMinutes}
            onChange={(e) => setDurationMinutes(Number(e.target.value))}
            required
          />
        </label>
        <button type="submit" disabled={submitting}>
          {submitting ? "Granting…" : "Grant break-glass"}
        </button>
        {error ? <p className={styles.error}>{error}</p> : null}
      </form>
      <ul className={styles.grants}>
        {grants.length === 0 ? (
          <li className={styles.empty}>No active grants.</li>
        ) : (
          grants.map((grant) => (
            <li key={grant.grant_id} className={styles.grantRow}>
              <BreakGlassTimerBadge grant={grant} />
              {grant.revoked_at ? null : (
                <button type="button" onClick={() => handleRevoke(grant.grant_id)}>
                  Revoke
                </button>
              )}
            </li>
          ))
        )}
      </ul>
    </section>
  );
}