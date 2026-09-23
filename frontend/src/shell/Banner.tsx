import { useEffect, useState } from "react";

import styles from "./Banner.module.css";

interface HealthResponse {
  synthetic_environment: boolean;
  app_env: string;
}

interface BannerProps {
  apiBase?: string;
}

/**
 * Synthetic-environment banner (AGENTS.md §18).
 *
 * Reads ``GET /api/v1/health`` once on mount and renders a visible
 * banner whenever ``synthetic_environment`` is true. The banner is
 * removed when the health check fails (we never want a false-negative
 * to silently hide the warning).
 *
 * Why a runtime health call rather than a build-time env: the health
 * endpoint is the authoritative source. An operator who turns off
 * SYNTHETIC_ENVIRONMENT at runtime sees the banner disappear the
 * next time the page reloads — no rebuild required.
 */
export function Banner({ apiBase = "/api/v1" }: BannerProps) {
  const [synthetic, setSynthetic] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${apiBase}/health`)
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then((data: HealthResponse) => {
        if (!cancelled) {
          setSynthetic(Boolean(data.synthetic_environment));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSynthetic(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase]);

  if (synthetic === false) {
    return null;
  }

  return (
    <div
      className={styles.banner}
      role="status"
      data-testid="synthetic-environment-banner"
      aria-live="polite"
    >
      <span className={styles.label}>SYNTHETIC ENVIRONMENT</span>
      <span className={styles.message}>
        No real data. Do not load enterprise records.
      </span>
    </div>
  );
}

export default Banner;