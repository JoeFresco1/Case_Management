import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiClientError, api, isForbiddenLike } from "../api/client";
import { bundleQueryKeys } from "./queryKeys";
import styles from "./Documents.module.css";

function parseIds(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiClientError) return error.message;
  return "Bundle operation failed.";
}

/**
 * Permissioned-bundle + share-by-link console (T5-US3, C-8).
 *
 * Bundles confer read access through the bundle only; membership never
 * makes bytes searchable to non-members (server-enforced). Reads are
 * metadata-only (name, member ids, roles). Share-link denials carry a
 * bare reason code so URL probes learn nothing about the bundle.
 *
 * The raw bearer token is rendered exactly once at mint time with an
 * explicit "copy now" warning — it is never stored or re-emitted.
 * Unknown/forbidden bundles render a metadata-free message: no name,
 * member ids, or grant roles leak.
 */
export function BundleSharing({ initialBundleId }: { initialBundleId?: string }) {
  const queryClient = useQueryClient();
  const [bundleId, setBundleId] = useState(initialBundleId ?? "");
  const [activeId, setActiveId] = useState(initialBundleId ?? "");
  const [name, setName] = useState("");
  const [members, setMembers] = useState("");
  const [grantee, setGrantee] = useState("");
  const [role, setRole] = useState("viewer");
  const [added, setAdded] = useState("");
  const [removed, setRemoved] = useState("");
  const [revokeReason, setRevokeReason] = useState("");
  const [ttlHours, setTtlHours] = useState("24");
  const [maxUses, setMaxUses] = useState("5");
  const [redeemToken, setRedeemToken] = useState("");
  const [revokeToken, setRevokeToken] = useState("");
  const [revokeTokenReason, setRevokeTokenReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const bundleQuery = useQuery({
    queryKey: activeId ? bundleQueryKeys.detail(activeId) : bundleQueryKeys.all,
    queryFn: () => api.bundles.get(activeId),
    enabled: activeId.length > 0,
    staleTime: 10_000,
  });

  const refreshBundle = () => {
    if (activeId) {
      void queryClient.invalidateQueries({ queryKey: bundleQueryKeys.detail(activeId) });
    }
  };

  const fail = (e: unknown) => setFailure(errorMessage(e));
  const ok = (message: string) => {
    setNotice(message);
    setFailure(null);
  };

  const create = useMutation({
    mutationFn: () =>
      api.bundles.create({ name: name.trim(), member_document_ids: parseIds(members) }),
    onSuccess: (bundle) => {
      setActiveId(bundle.id);
      setBundleId(bundle.id);
      ok(`Bundle ${bundle.id} created.`);
    },
    onError: fail,
  });

  const grant = useMutation({
    mutationFn: () =>
      api.bundles.grant(activeId, {
        grantee_id: grantee.trim(),
        role: role as "viewer" | "editor",
      }),
    onSuccess: () => {
      setGrantee("");
      ok("Grant recorded.");
      refreshBundle();
    },
    onError: fail,
  });

  const changeMembers = useMutation({
    mutationFn: () =>
      api.bundles.changeMembers(activeId, {
        added: parseIds(added),
        removed: parseIds(removed),
      }),
    onSuccess: () => {
      setAdded("");
      setRemoved("");
      ok("Bundle members updated.");
      refreshBundle();
    },
    onError: fail,
  });

  const revoke = useMutation({
    mutationFn: () => api.bundles.revoke(activeId, revokeReason.trim()),
    onSuccess: () => {
      setRevokeReason("");
      ok("Bundle revoked — grants and links no longer authorize.");
      refreshBundle();
    },
    onError: fail,
  });

  const mint = useMutation({
    mutationFn: () => {
      const hours = Number(ttlHours);
      const uses = Number(maxUses);
      return api.bundles.mintLink(activeId, {
        ttl_seconds: Math.round((Number.isFinite(hours) ? hours : 24) * 3600),
        max_uses: Number.isFinite(uses) && uses > 0 ? Math.round(uses) : 1,
      });
    },
    onSuccess: () => ok(`Link minted — copy the token now, it is shown once.`),
    onError: fail,
  });

  const redeem = useMutation({
    mutationFn: () => api.bundles.redeemLink(redeemToken.trim()),
    onSuccess: (result) =>
      ok(`Redeemed bundle ${result.bundle_id} (${result.member_document_ids.length} members).`),
    onError: fail,
  });

  const revokeLink = useMutation({
    mutationFn: () => api.bundles.revokeLink(revokeToken.trim(), revokeTokenReason.trim()),
    onSuccess: (result) => {
      setRevokeToken("");
      setRevokeTokenReason("");
      ok(`Link ${result.link_id} revoked.`);
    },
    onError: fail,
  });

  return (
    <section className={styles.viewer} aria-label="Bundle sharing">
      <h2>Bundle sharing</h2>
      <p className={styles.meta}>
        Permissioned bundles share metadata only; links are bearer tokens shown
        once. Denials never confirm what sits behind a link.
      </p>

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <h3>Create bundle</h3>
        <label>
          Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
          />
        </label>
        <label>
          Member document ids (comma or whitespace separated)
          <input
            value={members}
            onChange={(event) => setMembers(event.target.value)}
            placeholder="document uuid, …"
          />
        </label>
        <button type="submit" disabled={!name.trim() || create.isPending}>
          {create.isPending ? "Creating…" : "Create bundle"}
        </button>
      </form>

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          setActiveId(bundleId.trim());
          setNotice(null);
          setFailure(null);
        }}
      >
        <h3>Open bundle</h3>
        <label>
          Bundle id
          <input
            value={bundleId}
            onChange={(event) => setBundleId(event.target.value)}
            placeholder="bundle uuid"
          />
        </label>
        <button type="submit" disabled={!bundleId.trim()}>
          Open
        </button>
      </form>

      {activeId && bundleQuery.isLoading && <p>Loading bundle…</p>}
      {activeId && bundleQuery.error && isForbiddenLike(bundleQuery.error) && (
        <p className={styles.empty} role="alert">
          Bundle is restricted or not available.
        </p>
      )}
      {activeId && bundleQuery.error && !isForbiddenLike(bundleQuery.error) && (
        <p className={styles.error} role="alert">
          Bundle could not be loaded.
        </p>
      )}
      {activeId && bundleQuery.data && (
        <div className={styles.detail}>
          <strong>{bundleQuery.data.name}</strong>
          <div className={styles.meta}>
            {bundleQuery.data.status} · {bundleQuery.data.member_document_ids.length}{" "}
            member(s)
          </div>
          <ul className={styles.list}>
            {bundleQuery.data.member_document_ids.map((id) => (
              <li key={id} className={styles.item}>
                <code>{id}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeId && (
        <>
          <form
            className={styles.form}
            onSubmit={(event) => {
              event.preventDefault();
              grant.mutate();
            }}
          >
            <h3>Grant access</h3>
            <label>
              Grantee user id
              <input
                value={grantee}
                onChange={(event) => setGrantee(event.target.value)}
                placeholder="user uuid"
                required
              />
            </label>
            <label>
              Role
              <select value={role} onChange={(event) => setRole(event.target.value)}>
                <option value="viewer">viewer</option>
                <option value="editor">editor</option>
              </select>
            </label>
            <button type="submit" disabled={!grantee.trim() || grant.isPending}>
              {grant.isPending ? "Granting…" : "Grant"}
            </button>
          </form>

          <form
            className={styles.form}
            onSubmit={(event) => {
              event.preventDefault();
              changeMembers.mutate();
            }}
          >
            <h3>Change members</h3>
            <label>
              Add (ids)
              <input value={added} onChange={(event) => setAdded(event.target.value)} />
            </label>
            <label>
              Remove (ids)
              <input
                value={removed}
                onChange={(event) => setRemoved(event.target.value)}
              />
            </label>
            <button type="submit" disabled={changeMembers.isPending}>
              {changeMembers.isPending ? "Updating…" : "Update members"}
            </button>
          </form>

          <form
            className={styles.form}
            onSubmit={(event) => {
              event.preventDefault();
              mint.mutate();
            }}
          >
            <h3>Share by link</h3>
            <label>
              TTL (hours)
              <input
                type="number"
                min={1}
                max={720}
                value={ttlHours}
                onChange={(event) => setTtlHours(event.target.value)}
              />
            </label>
            <label>
              Max uses
              <input
                type="number"
                min={1}
                value={maxUses}
                onChange={(event) => setMaxUses(event.target.value)}
              />
            </label>
            <button type="submit" disabled={mint.isPending}>
              {mint.isPending ? "Minting…" : "Mint link"}
            </button>
            {mint.data && (
              <div>
                <p className={styles.meta}>
                  Copy now — the raw token is shown once and never stored:
                </p>
                <p className={styles.token}>{mint.data.raw_token}</p>
                <p className={styles.meta}>
                  Expires {new Date(mint.data.expires_at).toLocaleString()} · max{" "}
                  {mint.data.max_uses} use(s)
                </p>
              </div>
            )}
          </form>

          <form
            className={styles.form}
            onSubmit={(event) => {
              event.preventDefault();
              revoke.mutate();
            }}
          >
            <h3>Revoke bundle</h3>
            <label>
              Reason
              <input
                value={revokeReason}
                onChange={(event) => setRevokeReason(event.target.value)}
                required
              />
            </label>
            <button type="submit" disabled={!revokeReason.trim() || revoke.isPending}>
              {revoke.isPending ? "Revoking…" : "Revoke bundle"}
            </button>
          </form>
        </>
      )}

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          redeem.mutate();
        }}
      >
        <h3>Redeem link</h3>
        <label>
          Bearer token
          <input
            value={redeemToken}
            onChange={(event) => setRedeemToken(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={!redeemToken.trim() || redeem.isPending}>
          {redeem.isPending ? "Redeeming…" : "Redeem"}
        </button>
        {redeem.data && (
          <p className={styles.meta}>
            Bundle {redeem.data.bundle_id}: {redeem.data.member_document_ids.join(", ")}
          </p>
        )}
      </form>

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          revokeLink.mutate();
        }}
      >
        <h3>Revoke link</h3>
        <label>
          Bearer token
          <input
            value={revokeToken}
            onChange={(event) => setRevokeToken(event.target.value)}
            required
          />
        </label>
        <label>
          Reason
          <input
            value={revokeTokenReason}
            onChange={(event) => setRevokeTokenReason(event.target.value)}
            required
          />
        </label>
        <button
          type="submit"
          disabled={
            !revokeToken.trim() || !revokeTokenReason.trim() || revokeLink.isPending
          }
        >
          {revokeLink.isPending ? "Revoking…" : "Revoke link"}
        </button>
      </form>

      {notice && (
        <p className={styles.meta} role="status">
          {notice}
        </p>
      )}
      {failure && (
        <p className={styles.error} role="alert">
          {failure}
        </p>
      )}
    </section>
  );
}
