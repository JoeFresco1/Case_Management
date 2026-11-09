import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type { CaseSummary } from "../api/types";
import styles from "./CaseWorkspace.module.css";

/**
 * Renders the workflow actions available in the current case state.
 *
 * Availability and guard explanations come from the authorization-scoped
 * preflight endpoint; the execution endpoint remains authoritative.
 * Approval-gated transitions (FM-017) carry separation-of-duties
 * metadata (`approval_required`, `approval_eligible`, `approval_reason`)
 * so the worker sees *why* a consequential step needs a second actor
 * before attempting it — a self-approval attempt is a 403
 * `separation_of_duties_violation` with an explainable audit row.
 */
export function TransitionsPanel({ caseSummary }: { caseSummary: CaseSummary }) {
  const queryClient = useQueryClient();
  const transitionsQuery = useQuery({
    queryKey: ["transitions", caseSummary.id],
    queryFn: () => api.listTransitions(caseSummary.id),
  });
  const transitions = transitionsQuery.data?.transitions ?? [];
  const transitionMutation = useMutation({
    mutationFn: (transitionKey: string) => api.executeTransition(caseSummary.id, transitionKey),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      await queryClient.invalidateQueries({ queryKey: ["timeline", caseSummary.id] });
      await queryClient.invalidateQueries({ queryKey: ["transitions", caseSummary.id] });
    },
  });

  return (
    <section aria-labelledby="transitions-heading">
      <h2 id="transitions-heading">Available actions</h2>
      {transitionsQuery.isLoading && <p>Loading transitions…</p>}
      {transitionsQuery.error && (
        <p className={styles.error}>Transitions could not be loaded.</p>
      )}
      {!transitionsQuery.isLoading && !transitionsQuery.error && transitions.length === 0 && (
        <p className={styles.empty}>No transitions are available from this state.</p>
      )}
      {!transitionsQuery.isLoading && !transitionsQuery.error && transitions.length > 0 && (
        <ul className={styles.transitionList}>
          {transitions.map((option) => {
            const needsSecondActor =
              option.approval_required && !option.approval_eligible;
            return (
              <li key={option.key} className={styles.transitionItem}>
                <div>
                  <strong>{option.key.split("_").join(" ")}</strong>
                  {option.approval_required ? (
                    <span className={styles.meta}> · requires approval</span>
                  ) : null}
                  <div className={styles.meta}>{option.from_state.split("_").join(" ")} → {option.to_state.split("_").join(" ")}</div>
                  <div className={styles.guard}>
                    Guard: {option.allowed
                      ? "satisfied"
                      : "blocked" + (option.reason ? " — " + option.reason : "")}
                  </div>
                  {option.reason === "missing_documents" && <p>Attach {typeof option.details?.classification === "string" ? "a " + option.details.classification + " document" : "the required document"} to continue. <a href="?view=documents">Open documents →</a></p>}
                  {option.approval_required ? (
                    <div className={styles.guard} role="note">
                      {option.approval_eligible
                        ? `Approval: eligible — ${option.approval_reason ?? "a different actor from the originator may approve."}`
                        : `Approval: needs a second actor — ${option.approval_reason ?? "separation_of_duties_violation: the originator cannot self-approve."}`}
                    </div>
                  ) : null}
                </div>
                <button
                  type="button"
                  disabled={
                    transitionMutation.isPending ||
                    !option.allowed ||
                    needsSecondActor
                  }
                  title={
                    needsSecondActor
                      ? "Blocked: this consequential step requires a different actor (separation of duties)."
                      : undefined
                  }
                  onClick={() => transitionMutation.mutate(option.key)}
                >
                  {transitionMutation.isPending ? "Applying…" : "Apply transition"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {transitionMutation.error && (
        <p className={styles.error} role="alert">
          {transitionMutation.error instanceof ApiClientError
            ? transitionMutation.error.message
            : "Transition could not be applied."}
        </p>
      )}
      {transitionMutation.data && (
        <p role="status">Transition applied: {transitionMutation.data.from_state} → {transitionMutation.data.to_state}</p>
      )}
    </section>
  );
}
