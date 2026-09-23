/** Read-only workflow administration API seam.
 *
 * The request/response shapes are intentionally centralized here while the
 * parent regenerates ``types.gen.ts`` after the new GET endpoints land.
 * No mutation helper is exposed: v1 administration is inspection plus dry-run.
 */

export interface WorkflowSummary {
  id: string;
  key: string;
  name: string;
  description: string;
  current_version_id: string | null;
  current_version_number: number | null;
}

export interface WorkflowVersionProjection {
  workflow_definition_id: string;
  workflow_version_id: string;
  version_number: number;
  status: "draft" | "published" | "retired";
  states: Array<{ key: string; label: string; is_initial: boolean; is_terminal: boolean }>;
  transitions: Array<{
    key: string;
    from_state: string;
    to_state: string;
    human_driven: boolean;
    guard: Record<string, unknown> | null;
    actions: Array<Record<string, unknown>>;
  }>;
}

export interface WorkflowDiff {
  workflow_definition_id: string;
  version_number: number;
  previous_version_number: number | null;
  added_states: string[];
  removed_states: string[];
  added_transitions: string[];
  removed_transitions: string[];
}

export interface SimulationResult {
  initial_state: string;
  final_state: string;
  outcome: "completed" | "blocked" | "exception";
  steps?: Array<{
    transition: string;
    from_state: string;
    to_state: string;
    guards: Array<{ rule: Record<string, unknown>; inputs: Record<string, unknown>; result: boolean }>;
    actions_fired: Array<Record<string, unknown>>;
  }>;
  blockers?: Array<{ kind: string; message: string; transition?: string; state?: string }>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(`Workflow request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export function listWorkflows(): Promise<WorkflowSummary[]> {
  return request<WorkflowSummary[]>("/api/v1/admin/workflows");
}

export function getWorkflowVersion(
  workflowId: string,
  versionNumber: number,
): Promise<WorkflowVersionProjection> {
  return request<WorkflowVersionProjection>(
    `/api/v1/admin/workflows/${encodeURIComponent(workflowId)}/versions/${versionNumber}`,
  );
}

export function getWorkflowDiff(workflowId: string, versionNumber: number): Promise<WorkflowDiff> {
  return request<WorkflowDiff>(
    `/api/v1/admin/workflows/${encodeURIComponent(workflowId)}/versions/${versionNumber}/diff`,
  );
}

export function simulateWorkflow(
  workflowId: string,
  versionNumber: number,
  syntheticCase: Record<string, unknown>,
): Promise<SimulationResult> {
  return request<SimulationResult>(
    `/api/v1/admin/workflows/${encodeURIComponent(workflowId)}/versions/${versionNumber}/simulate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ synthetic_case: syntheticCase, actions: [] }),
    },
  );
}
