import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { ApiClientError } from "../api/client";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Records-admin console for archival + freeze + orphans (P08-US5, US7, US8 / FM-041).
 *
 * The page surfaces three operator surfaces:
 *
 * 1. **Archival** — list frozen cases; archive a case to the warmer tier
 *    with a required rationale; lift the freeze on a previously archived
 *    case. The case itself is not deleted — the move is a tier change
 *    that freezes the case for the duration of the archive.
 * 2. **Disposal freeze** — declare a tenant-wide freeze window during
 *    which the disposition approval pipeline refuses approvals. An admin
 *    can cancel an active freeze before its natural ``ends_at`` expires.
 *    The freeze does NOT auto-delete anything — it only blocks the
 *    disposition pipeline.
 * 3. **Orphan proposals** — run the orphan-detection scan to surface
 *    reviewable proposals. The scan never auto-deletes a case
 *    (Constitution IV); a reviewer either surfaces the proposal (the
 *    case moves into the normal disposition pipeline) or dismisses it
 *    with a required rationale.
 */

type ArchivalTierName = "hot" | "warm";
type ArchivalStatusName = "archived" | "unfrozen";
type OrphanProposalStatusName = "open" | "surfaced" | "dismissed";

interface CaseArchival {
  id: string;
  tenant_id: string;
  case_id: string;
  tier: ArchivalTierName;
  archived_at: string;
  archived_by: string;
  reason: string;
  status: ArchivalStatusName;
  unfrozen_at: string | null;
  unfrozen_by: string | null;
  unfreeze_reason: string | null;
}

interface DisposalFreeze {
  id: string;
  tenant_id: string;
  starts_at: string;
  ends_at: string;
  reason: string;
  created_at: string;
  created_by: string;
  cancelled_at: string | null;
  cancelled_by: string | null;
  cancellation_reason: string | null;
}

interface OrphanProposal {
  id: string;
  tenant_id: string;
  case_id: string;
  detected_at: string;
  last_activity_at: string;
  reason: string;
  status: OrphanProposalStatusName;
  reviewed_at: string | null;
  reviewed_by: string | null;
  review_reason: string | null;
}

interface OrphanProposalEntry {
  proposal_id: string;
  case_id: string;
  case_reference: string;
  detected_at: string;
  last_activity_at: string;
  reason: string;
  status: OrphanProposalStatusName;
}

interface OrphanScanResponse {
  items: OrphanProposalEntry[];
  skipped_holds: string[];
}

const ADMIN_HEADERS: Record<string, string> = {
  "X-Correlation-Id": "00000000-0000-0000-0000-000000000000",
  "X-Dev-User": "00000000-0000-0000-0000-000000000001",
  "X-Dev-Tenant": "00000000-0000-0000-0000-000000000000",
};

function correlationId(): string {
  return crypto.randomUUID();
}

async function adminFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Correlation-Id": correlationId(),
      // Mutations require Idempotency-Key (backend 400 without it);
      // GETs omit it so the header never pollutes the read contract.
      ...(method === "GET" || method === "HEAD"
        ? {}
        : { "Idempotency-Key": correlationId() }),
      ...ADMIN_HEADERS,
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let body: { message?: string } = {};
    try {
      body = await response.json();
    } catch {
      // ignore parse errors
    }
    throw new ApiClientError(
      body.message ?? `request failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

export function Archival() {
  return (
    <section>
      <h2>Archival &amp; retention</h2>
      <p className={styles.meta}>
        Synthetic-environment console for the records-management pillar. The
        actions below move a case to a cooler storage tier, declare a
        tenant-wide disposal freeze, and surface reviewable orphan
        proposals. No auto-deletion: every state change is explicit and
        audited.
      </p>
      <ArchivalPanel />
      <FreezePanel />
      <OrphanPanel />
    </section>
  );
}

function ArchivalPanel() {
  const queryClient = useQueryClient();
  const [caseId, setCaseId] = useState("");
  const [tier, setTier] = useState<ArchivalTierName>("warm");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const activeQuery = useQuery({
    queryKey: ["retention", "archival", "list"],
    queryFn: () => Promise.resolve([] as CaseArchival[]),
    enabled: false,
  });

  const archive = useMutation({
    mutationFn: async () =>
      adminFetch<CaseArchival>(
        `/api/v1/admin/archival/cases/${encodeURIComponent(caseId)}/archive`,
        {
          method: "POST",
          body: JSON.stringify({ tier, reason }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setError(null);
      setReason("");
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  const unfreeze = useMutation({
    mutationFn: async (targetCaseId: string) =>
      adminFetch<CaseArchival>(
        `/api/v1/admin/archival/cases/${encodeURIComponent(targetCaseId)}/unfreeze`,
        {
          method: "POST",
          body: JSON.stringify({ reason }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setError(null);
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  function onArchive(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!caseId.trim()) {
      setError("case id is required");
      return;
    }
    if (!reason.trim()) {
      setError("reason is required");
      return;
    }
    archive.mutate();
  }

  return (
    <article>
      <h3>Archival</h3>
      <p className={styles.meta}>
        Move a case to a cooler storage tier. The case is frozen for the
        duration of the archive — disposition approvals are refused while
        the case is frozen. The move is explicit and audited.
      </p>
      <form onSubmit={onArchive} className={styles.form}>
        <label>
          Case id
          <input
            type="text"
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
            placeholder="UUID of the case to archive"
            required
          />
        </label>
        <label>
          Tier
          <select
            value={tier}
            onChange={(e) => setTier(e.target.value as ArchivalTierName)}
          >
            <option value="warm">warm (cooler tier)</option>
          </select>
        </label>
        <label>
          Reason
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="why is this case being archived?"
            required
            minLength={1}
            maxLength={1024}
          />
        </label>
        <button type="submit" disabled={archive.isPending}>
          {archive.isPending ? "Archiving…" : "Archive case"}
        </button>
      </form>
      {error ? <p className={styles.error}>{error}</p> : null}
      {archive.data ? (
        <p className={styles.meta}>
          Archived <code>{archive.data.case_id.slice(0, 8)}</code> in{" "}
          <code>{archive.data.tier}</code> tier. Unfreeze with the same
          endpoint by providing a rationale.
        </p>
      ) : null}
      {activeQuery.data && activeQuery.data.length > 0 ? (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Case</th>
              <th>Tier</th>
              <th>Status</th>
              <th>Reason</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {activeQuery.data.map((row) => (
              <tr key={row.id}>
                <td>
                  <code>{row.case_id.slice(0, 8)}</code>
                </td>
                <td>
                  <code>{row.tier}</code>
                </td>
                <td>
                  <code>{row.status}</code>
                </td>
                <td>{row.reason}</td>
                <td>
                  {row.status === "archived" ? (
                    <button
                      type="button"
                      disabled={unfreeze.isPending || !reason.trim()}
                      onClick={() => unfreeze.mutate(row.case_id)}
                    >
                      Unfreeze
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </article>
  );
}

function FreezePanel() {
  const queryClient = useQueryClient();
  const [startsAt, setStartsAt] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [reason, setReason] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const activeQuery = useQuery({
    queryKey: ["retention", "disposal-freezes", "active"],
    queryFn: () =>
      adminFetch<{ items: DisposalFreeze[] }>(
        "/api/v1/admin/disposal-freezes/active",
      ),
    refetchInterval: 5_000,
  });

  const declare = useMutation({
    mutationFn: async () =>
      adminFetch<DisposalFreeze>("/api/v1/admin/disposal-freezes", {
        method: "POST",
        body: JSON.stringify({
          starts_at: new Date(startsAt).toISOString(),
          ends_at: new Date(endsAt).toISOString(),
          reason,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setError(null);
      setReason("");
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  const cancel = useMutation({
    mutationFn: async (freezeId: string) =>
      adminFetch<DisposalFreeze>(
        `/api/v1/admin/disposal-freezes/${encodeURIComponent(freezeId)}/cancel`,
        {
          method: "POST",
          body: JSON.stringify({ reason: cancelReason }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setError(null);
      setCancelReason("");
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  function onDeclare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!startsAt || !endsAt) {
      setError("starts_at and ends_at are required");
      return;
    }
    if (!reason.trim()) {
      setError("reason is required");
      return;
    }
    declare.mutate();
  }

  return (
    <article>
      <h3>Disposal freeze</h3>
      <p className={styles.meta}>
        Declare a tenant-wide freeze window. The disposition approval
        pipeline refuses approvals while any active freeze covers the
        evaluation moment. The freeze does NOT auto-delete anything — it
        only blocks the pipeline.
      </p>
      <form onSubmit={onDeclare} className={styles.form}>
        <label>
          Starts at
          <input
            type="datetime-local"
            value={startsAt}
            onChange={(e) => setStartsAt(e.target.value)}
            required
          />
        </label>
        <label>
          Ends at
          <input
            type="datetime-local"
            value={endsAt}
            onChange={(e) => setEndsAt(e.target.value)}
            required
          />
        </label>
        <label>
          Reason
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="litigation / regulatory window"
            required
            minLength={1}
            maxLength={1024}
          />
        </label>
        <button type="submit" disabled={declare.isPending}>
          {declare.isPending ? "Declaring…" : "Declare freeze"}
        </button>
      </form>
      {error ? <p className={styles.error}>{error}</p> : null}
      {activeQuery.isLoading ? <p>Loading active freezes…</p> : null}
      {activeQuery.data ? (
        activeQuery.data.items.length === 0 ? (
          <p>No active freezes.</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Window</th>
                <th>Reason</th>
                <th>Created</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {activeQuery.data.items.map((freeze) => (
                <tr key={freeze.id}>
                  <td>
                    <code>
                      {new Date(freeze.starts_at).toLocaleString()} →{" "}
                      {new Date(freeze.ends_at).toLocaleString()}
                    </code>
                  </td>
                  <td>{freeze.reason}</td>
                  <td>{new Date(freeze.created_at).toLocaleString()}</td>
                  <td>
                    <label className={styles.meta}>
                      Cancel reason
                      <input
                        type="text"
                        value={cancelReason}
                        onChange={(e) => setCancelReason(e.target.value)}
                        placeholder="why is the freeze being lifted?"
                        minLength={1}
                        maxLength={1024}
                      />
                    </label>
                    <button
                      type="button"
                      disabled={cancel.isPending || !cancelReason.trim()}
                      onClick={() => cancel.mutate(freeze.id)}
                    >
                      {cancel.isPending ? "Cancelling…" : "Cancel"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : null}
    </article>
  );
}

function OrphanPanel() {
  const queryClient = useQueryClient();
  const [reviewReason, setReviewReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lastScan, setLastScan] = useState<OrphanScanResponse | null>(null);

  const openQuery = useQuery({
    queryKey: ["retention", "orphan-proposals", "open"],
    queryFn: () =>
      adminFetch<{ items: OrphanProposalEntry[] }>(
        "/api/v1/admin/orphan-proposals",
      ),
    refetchInterval: 5_000,
  });

  const scan = useMutation({
    mutationFn: async () =>
      adminFetch<OrphanScanResponse>("/api/v1/admin/orphan-proposals/scan", {
        method: "POST",
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setLastScan(data);
      setError(null);
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  const review = useMutation({
    mutationFn: async (input: {
      proposalId: string;
      decision: "surface" | "dismiss";
    }) =>
      adminFetch<OrphanProposal>(
        `/api/v1/admin/orphan-proposals/${encodeURIComponent(input.proposalId)}/review`,
        {
          method: "POST",
          body: JSON.stringify({
            decision: input.decision,
            reason: reviewReason,
          }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention"] });
      setError(null);
      setReviewReason("");
    },
    onError: (err) => {
      setError(err instanceof ApiClientError ? err.message : String(err));
    },
  });

  function reviewDismiss(proposalId: string) {
    if (!reviewReason.trim()) {
      setError("a rationale is required to dismiss an orphan proposal");
      return;
    }
    review.mutate({ proposalId, decision: "dismiss" });
  }

  return (
    <article>
      <h3>Orphan proposals</h3>
      <p className={styles.meta}>
        The orphan-detection scan surfaces reviewable proposals for cases
        past their retention horizon AND with no recent activity. The scan
        never auto-deletes a case (Constitution IV). A reviewer either
        surfaces the proposal (the case moves into the normal disposition
        pipeline) or dismisses it with a required rationale.
      </p>
      <button
        type="button"
        disabled={scan.isPending}
        onClick={() => scan.mutate()}
      >
        {scan.isPending ? "Scanning…" : "Run orphan scan"}
      </button>
      {lastScan ? (
        <p className={styles.meta}>
          Surfaced <strong>{lastScan.items.length}</strong> proposal(s) ·
          skipped <strong>{lastScan.skipped_holds.length}</strong> case(s)
          under legal hold.
        </p>
      ) : null}
      {error ? <p className={styles.error}>{error}</p> : null}
      {openQuery.isLoading ? <p>Loading open proposals…</p> : null}
      {openQuery.data ? (
        openQuery.data.items.length === 0 ? (
          <p>No open proposals.</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Case</th>
                <th>Last activity</th>
                <th>Reason</th>
                <th>Rationale</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {openQuery.data.items.map((proposal) => (
                <tr key={proposal.proposal_id}>
                  <td>
                    <code>
                      {proposal.case_reference
                        ? proposal.case_reference
                        : proposal.case_id.slice(0, 8)}
                    </code>
                  </td>
                  <td>
                    {new Date(proposal.last_activity_at).toLocaleString()}
                  </td>
                  <td>{proposal.reason}</td>
                  <td>
                    <input
                      type="text"
                      value={reviewReason}
                      onChange={(e) => setReviewReason(e.target.value)}
                      placeholder="required for dismiss"
                      maxLength={1024}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      disabled={review.isPending}
                      onClick={() =>
                        review.mutate({
                          proposalId: proposal.proposal_id,
                          decision: "surface",
                        })
                      }
                    >
                      Surface
                    </button>{" "}
                    <button
                      type="button"
                      disabled={review.isPending || !reviewReason.trim()}
                      onClick={() => reviewDismiss(proposal.proposal_id)}
                    >
                      Dismiss
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : null}
    </article>
  );
}
