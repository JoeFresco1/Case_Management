import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError, api, isForbiddenLike } from "../api/client";
import { documentQueryKeys } from "./queryKeys";
import styles from "./Documents.module.css";

function conflictDetail(error: unknown): { holder?: string; expiresAt?: string } {
  if (error instanceof ApiClientError && error.status === 409) {
    const details = error.details ?? {};
    const holder = typeof details["holder"] === "string" ? details["holder"] : undefined;
    const expiresAt =
      typeof details["expires_at"] === "string" ? details["expires_at"] : undefined;
    return { holder, expiresAt };
  }
  return {};
}

/**
 * Checkout indicator + checkout/checkin actions (T5 viewer pipeline).
 *
 * A document has at most one live (unreleased, unexpired) checkout.
 * The indicator derives liveness client-side from the history trail:
 * unreleased with `expires_at` in the future. A 409 conflict carries
 * the holder + expiry only — safe metadata the UI may render.
 *
 * Forbidden/unknown documents render a metadata-free message: no
 * holder, expiry, or history leaks.
 */
export function CheckoutIndicator({ documentId }: { documentId: string }) {
  const queryClient = useQueryClient();
  const [leaseHours, setLeaseHours] = useState("4");

  const historyQuery = useQuery({
    queryKey: documentQueryKeys.checkoutHistory(documentId),
    queryFn: () => api.viewer.checkoutHistory(documentId, { limit: 10 }),
    staleTime: 10_000,
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({
      queryKey: documentQueryKeys.checkoutHistory(documentId),
    });

  const checkout = useMutation({
    mutationFn: () => {
      const hours = Number(leaseHours);
      const leaseSeconds =
        Number.isFinite(hours) && hours > 0 ? Math.round(hours * 3600) : undefined;
      return api.viewer.checkout(documentId, leaseSeconds);
    },
    onSuccess: () => invalidate(),
  });

  const checkin = useMutation({
    mutationFn: () => api.viewer.checkin(documentId),
    onSuccess: () => invalidate(),
  });

  if (historyQuery.isLoading) return <p>Loading checkout status…</p>;
  if (historyQuery.error) {
    if (isForbiddenLike(historyQuery.error)) {
      return (
        <p className={styles.empty} role="alert">
          Checkout status is restricted or not available.
        </p>
      );
    }
    return (
      <p className={styles.error} role="alert">
        Checkout status could not be loaded.
      </p>
    );
  }

  const trail = historyQuery.data ?? [];
  const now = Date.now();
  const live = trail.find(
    (lock) => lock.released_at === null && new Date(lock.expires_at).getTime() > now,
  );

  const conflict = conflictDetail(checkout.error ?? checkin.error);

  return (
    <section aria-label="Checkout status">
      <h3>Checkout</h3>
      {live ? (
        <p>
          <span className={styles.holder}>Checked out</span> by{" "}
          <code>{live.holder_user_id}</code> until{" "}
          <time dateTime={live.expires_at}>
            {new Date(live.expires_at).toLocaleString()}
          </time>
          {live.renewed && <span className={styles.badge}>renewed</span>}
        </p>
      ) : (
        <p className={styles.empty}>Available — no live checkout.</p>
      )}
      <div className={styles.inlineRow}>
        <label>
          Lease (hours)
          <input
            type="number"
            min={1}
            max={168}
            value={leaseHours}
            onChange={(event) => setLeaseHours(event.target.value)}
            aria-label="Checkout lease in hours"
          />
        </label>
        <button type="button" disabled={checkout.isPending} onClick={() => checkout.mutate()}>
          {checkout.isPending ? "Checking out…" : "Check out"}
        </button>
        <button type="button" disabled={checkin.isPending} onClick={() => checkin.mutate()}>
          {checkin.isPending ? "Checking in…" : "Check in"}
        </button>
      </div>
      {(checkout.error ?? checkin.error) && (
        <p className={styles.error} role="alert">
          {(checkout.error ?? checkin.error) instanceof ApiClientError
            ? (checkout.error ?? checkin.error)?.message
            : "Checkout operation failed."}{" "}
          {conflict.holder && (
            <span>
              Held by <code>{conflict.holder}</code>
              {conflict.expiresAt &&
                ` until ${new Date(conflict.expiresAt).toLocaleString()}`}
            </span>
          )}
        </p>
      )}
      {trail.length > 0 && (
        <ol className={styles.list}>
          {trail.slice(0, 5).map((lock) => (
            <li key={lock.id} className={styles.item}>
              <code>{lock.holder_user_id}</code>
              <div className={styles.meta}>
                acquired {new Date(lock.acquired_at).toLocaleString()} · expires{" "}
                {new Date(lock.expires_at).toLocaleString()}
                {lock.released_at
                  ? ` · released ${new Date(lock.released_at).toLocaleString()}`
                  : " · active"}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
