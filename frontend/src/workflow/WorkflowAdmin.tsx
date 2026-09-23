/** Read-only workflow administration view (T079 / FM-013 / FM-015).
 *
 * Administrators can inspect tenant-scoped workflow versions, compare a
 * version with its predecessor, and run a synthetic dry-run. Authoring and
 * publication controls are deliberately absent from this v1 surface.
 */
import { useEffect, useState } from "react";

import {
  getWorkflowDiff,
  getWorkflowVersion,
  listWorkflows,
  simulateWorkflow,
  type SimulationResult,
  type WorkflowDiff,
  type WorkflowSummary,
  type WorkflowVersionProjection,
} from "./api";

export function WorkflowAdmin() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [selected, setSelected] = useState<WorkflowSummary | null>(null);
  const [version, setVersion] = useState<WorkflowVersionProjection | null>(null);
  const [diff, setDiff] = useState<WorkflowDiff | null>(null);
  const [simulation, setSimulation] = useState<SimulationResult | null>(null);
  const [syntheticCase, setSyntheticCase] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listWorkflows().then(setWorkflows).catch(() => setError("Unable to load workflows."));
  }, []);

  async function inspect(workflow: WorkflowSummary) {
    setSelected(workflow);
    setVersion(null);
    setDiff(null);
    setSimulation(null);
    setError(null);
    if (!workflow.current_version_id) return;
    try {
      const current = await getWorkflowVersion(workflow.id, workflow.current_version_number ?? 1);
      setVersion(current);
      setDiff(await getWorkflowDiff(workflow.id, current.version_number));
    } catch {
      setError("Unable to load the selected workflow version.");
    }
  }

  async function runSimulation() {
    if (!selected || !version) return;
    try {
      const parsed: unknown = JSON.parse(syntheticCase);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("Synthetic case must be a JSON object.");
      }
      setSimulation(await simulateWorkflow(selected.id, version.version_number, parsed as Record<string, unknown>));
      setError(null);
    } catch {
      setError("Simulation requires a valid JSON object.");
    }
  }

  return (
    <main data-testid="workflow-admin">
      <h1>Workflow administration</h1>
      <p>Read-only configuration inspection and synthetic simulation.</p>
      {error ? <p role="alert">{error}</p> : null}
      <section aria-label="Workflows">
        <h2>Workflows</h2>
        {workflows.length === 0 ? <p>No workflows available.</p> : (
          <ul>
            {workflows.map((workflow) => (
              <li key={workflow.id}>
                <button type="button" onClick={() => void inspect(workflow)}>{workflow.name} ({workflow.key})</button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {version ? (
        <section aria-label="Workflow version">
          <h2>{selected?.name} · version {version.version_number} ({version.status})</h2>
          <h3>States</h3>
          <ul>{version.states.map((state) => <li key={state.key}>{state.label} <code>{state.key}</code></li>)}</ul>
          <h3>Transitions</h3>
          <ul>{version.transitions.map((transition) => <li key={transition.key}>{transition.from_state} → {transition.to_state} <code>{transition.key}</code></li>)}</ul>
          {diff ? <pre data-testid="workflow-diff">{JSON.stringify(diff, null, 2)}</pre> : null}
          <h3>Simulator</h3>
          <textarea aria-label="Synthetic case JSON" value={syntheticCase} onChange={(event) => setSyntheticCase(event.target.value)} />
          <button type="button" onClick={() => void runSimulation()}>Run simulation</button>
          {simulation ? <pre data-testid="simulation-trace">{JSON.stringify(simulation, null, 2)}</pre> : null}
        </section>
      ) : null}
    </main>
  );
}

export default WorkflowAdmin;
