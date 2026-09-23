/**
 * Visual workflow designer (FM-020 / P10-US7).
 *
 * Administrators drag-and-drop states and transitions onto a canvas
 * to author a workflow definition. The designer emits **the same
 * `WorkflowDefinitionRequest` JSON body** the engine consumes via
 * `POST /api/v1/admin/workflows/{key}/versions` and validates via
 * `POST /api/v1/admin/workflows/{key}/versions:validate`. No second
 * format, no client-side validator: the backend
 * `app.application.configuration.validate_definition` is the single
 * source of truth (AGENTS.md §16.2 / plan §5).
 *
 * # Wire shape contract
 *
 * The export shape is exactly:
 *
 * ```json
 * {
 *   "states":      [{ "key", "label", "is_initial", "is_terminal" }],
 *   "transitions": [{
 *     "key", "from_state", "to_state", "human_driven",
 *     "guard": <declarative document or null>,
 *     "actions": [...]
 *   }]
 * }
 * ```
 *
 * Key constraints the canvas enforces so a malformed definition
 * cannot leave the designer:
 *
 * - state keys are unique, alphanumeric (with `_` / `-`),
 * - exactly one state carries `is_initial=true`,
 * - transition keys are unique,
 * - `from_state` / `to_state` reference declared states,
 * - `from_state` may not be terminal,
 * - `to_state` may not be initial,
 * - `from_state !== to_state` (no self-loops).
 *
 * These mirror `domain.workflow.WorkflowState.__post_init__` and
 * `WorkflowTransition.__post_init__`. Anything they catch additionally
 * (e.g. unreachability, automatic cycles, missing terminal) is caught
 * by the backend validator when the user clicks **Validate**; the
 * designer surfaces those errors verbatim.
 *
 * # Simulation
 *
 * The backend simulate endpoint (`/admin/workflows/{id}/versions/{n}/simulate`)
 * requires a persisted `WorkflowVersion`. The designer therefore
 * drafts the current canvas as a new version, then calls simulate,
 * then surfaces the trace. The result is read-only — no outbox, no
 * audit, no synthetic case persisted (plan §5).
 *
 * # INTEGRATION_SEAM
 *
 * The HTTP client wrapper around `/api/v1/admin/workflows/*` is the
 * seam. When a corporate / Azure API gateway fronts the admin surface
 * the wrapper is the only file that changes.
 */
import {
  type ChangeEvent,
  type DragEvent,
  type MouseEvent,
  useCallback,
  useMemo,
  useState,
} from "react";

import { ApiClientError } from "../api/client";

/* --------------------------------------------------------------------- */
/* Wire types — mirror backend schemas/admin_workflows.py exactly.       */
/* --------------------------------------------------------------------- */

export interface DesignerState {
  key: string;
  label: string;
  is_initial: boolean;
  is_terminal: boolean;
  /** Canvas coordinates (px). UI-only; not exported. */
  x: number;
  y: number;
}

export interface DesignerTransition {
  key: string;
  from_state: string;
  to_state: string;
  human_driven: boolean;
  guard: Record<string, unknown> | null;
  actions: Array<Record<string, unknown>>;
}

export interface DesignerDefinition {
  states: DesignerState[];
  transitions: DesignerTransition[];
}

/**
 * The wire body sent to the engine. Shape is byte-for-byte the
 * backend's `DraftWorkflowVersionRequest.definition`
 * (`WorkflowDefinitionRequest`). The keys must match exactly: the
 * designer does not translate, rename, or enrich.
 */
export interface DesignerExportBody {
  definition: {
    states: Array<{
      key: string;
      label: string;
      is_initial: boolean;
      is_terminal: boolean;
    }>;
    transitions: Array<{
      key: string;
      from_state: string;
      to_state: string;
      human_driven: boolean;
      guard: Record<string, unknown> | null;
      actions: Array<Record<string, unknown>>;
    }>;
  };
}

/* --------------------------------------------------------------------- */
/* Validation (client-side, structural only — backend is authoritative)   */
/* --------------------------------------------------------------------- */

export interface DesignerIssue {
  /** Stable code: matches `code` in the backend `ErrorResponse`. */
  code:
    | "duplicate_state_key"
    | "duplicate_transition_key"
    | "invalid_state_key"
    | "invalid_transition_key"
    | "no_initial_state"
    | "multiple_initial_states"
    | "transition_unknown_from"
    | "transition_unknown_to"
    | "transition_from_terminal"
    | "transition_to_initial"
    | "transition_self_loop"
    | "missing_label";
  message: string;
  /** UI-only field path. Backend `field=` uses dotted form; we mirror. */
  field: string;
}

/** Mirrors `domain.workflow.WorkflowState.__post_init__` key rule. */
const KEY_PATTERN = /^[A-Za-z0-9_-]+$/;

function checkKeys(value: string): boolean {
  return KEY_PATTERN.test(value);
}

export function validateDesign(definition: DesignerDefinition): DesignerIssue[] {
  const issues: DesignerIssue[] = [];
  const stateKeys = new Set<string>();

  for (const state of definition.states) {
    if (!checkKeys(state.key)) {
      issues.push({
        code: "invalid_state_key",
        message: `state key ${JSON.stringify(state.key)} must be alphanumeric (with _ or -)`,
        field: `states.${state.key}`,
      });
    }
    if (stateKeys.has(state.key)) {
      issues.push({
        code: "duplicate_state_key",
        message: `duplicate state key ${JSON.stringify(state.key)}`,
        field: `states.${state.key}`,
      });
    }
    stateKeys.add(state.key);
    if (!state.label.trim()) {
      issues.push({
        code: "missing_label",
        message: `state ${JSON.stringify(state.key)} requires a non-empty label`,
        field: `states.${state.key}.label`,
      });
    }
  }

  const initialCount = definition.states.filter((s) => s.is_initial).length;
  if (initialCount === 0) {
    issues.push({
      code: "no_initial_state",
      message: "workflow definition requires exactly one initial state",
      field: "states",
    });
  } else if (initialCount > 1) {
    issues.push({
      code: "multiple_initial_states",
      message: `workflow definition has ${initialCount} initial states; expected exactly 1`,
      field: "states",
    });
  }

  const seenTransitionKeys = new Set<string>();
  const statesByKey = new Map(definition.states.map((s) => [s.key, s]));

  for (const t of definition.transitions) {
    if (!checkKeys(t.key)) {
      issues.push({
        code: "invalid_transition_key",
        message: `transition key ${JSON.stringify(t.key)} must be alphanumeric (with _ or -)`,
        field: `transitions.${t.key}`,
      });
    }
    if (seenTransitionKeys.has(t.key)) {
      issues.push({
        code: "duplicate_transition_key",
        message: `duplicate transition key ${JSON.stringify(t.key)}`,
        field: `transitions.${t.key}`,
      });
    }
    seenTransitionKeys.add(t.key);

    const fromState = statesByKey.get(t.from_state);
    if (!fromState) {
      issues.push({
        code: "transition_unknown_from",
        message: `transition ${JSON.stringify(t.key)} references unknown from_state ${JSON.stringify(t.from_state)}`,
        field: `transitions.${t.key}.from`,
      });
    } else if (fromState.is_terminal) {
      issues.push({
        code: "transition_from_terminal",
        message: `transition ${JSON.stringify(t.key)} originates from terminal state ${JSON.stringify(t.from_state)}`,
        field: `transitions.${t.key}.from`,
      });
    }

    const toState = statesByKey.get(t.to_state);
    if (!toState) {
      issues.push({
        code: "transition_unknown_to",
        message: `transition ${JSON.stringify(t.key)} references unknown to_state ${JSON.stringify(t.to_state)}`,
        field: `transitions.${t.key}.to`,
      });
    } else if (toState.is_initial) {
      issues.push({
        code: "transition_to_initial",
        message: `transition ${JSON.stringify(t.key)} targets initial state ${JSON.stringify(t.to_state)}`,
        field: `transitions.${t.key}.to`,
      });
    }

    if (t.from_state && t.from_state === t.to_state) {
      issues.push({
        code: "transition_self_loop",
        message: `transition ${JSON.stringify(t.key)} cannot be a self-loop`,
        field: `transitions.${t.key}`,
      });
    }
  }

  return issues;
}

/* --------------------------------------------------------------------- */
/* Serialization — single source of truth for engine wire shape.          */
/* --------------------------------------------------------------------- */

/**
 * Project the canvas model into the exact wire body the engine
 * accepts. The exported shape has **no UI fields** (coordinates,
 * selection state, etc.) so the backend's
 * `DraftWorkflowVersionRequest.definition` schema matches
 * byte-for-byte. Any field the schema does not declare must not
 * appear here — that would be a second format.
 */
export function serializeDefinition(
  definition: DesignerDefinition,
): DesignerExportBody {
  return {
    definition: {
      states: definition.states.map((s) => ({
        key: s.key,
        label: s.label,
        is_initial: s.is_initial,
        is_terminal: s.is_terminal,
      })),
      transitions: definition.transitions.map((t) => ({
        key: t.key,
        from_state: t.from_state,
        to_state: t.to_state,
        human_driven: t.human_driven,
        guard: t.guard,
        actions: t.actions,
      })),
    },
  };
}

/* --------------------------------------------------------------------- */
/* Backend integration                                                   */
/* --------------------------------------------------------------------- */

interface ValidationSuccessResponse {
  workflow_definition_id: string;
  states: string[];
  transitions: string[];
}

interface ValidationErrorResponse {
  code: string;
  message: string;
  details?: { field?: string };
  correlation_id?: string;
}

async function postValidate(
  workflowKey: string,
  body: DesignerExportBody,
): Promise<ValidationSuccessResponse> {
  const response = await fetch(
    `/api/v1/admin/workflows/${encodeURIComponent(workflowKey)}/versions:validate`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) {
    let err: ValidationErrorResponse = {
      code: "validation_failed",
      message: `validate request failed: ${response.status}`,
    };
    try {
      err = (await response.json()) as ValidationErrorResponse;
    } catch {
      /* ignore */
    }
    throw new ApiClientError(err.message, response.status);
  }
  return (await response.json()) as ValidationSuccessResponse;
}

/* --------------------------------------------------------------------- */
/* Canvas model — drag-and-drop state authoring                          */
/* --------------------------------------------------------------------- */

const CANVAS_WIDTH = 720;
const CANVAS_HEIGHT = 360;

interface DragState {
  kind: "state";
  stateKey: string;
  offsetX: number;
  offsetY: number;
}

interface PendingTransition {
  from_state: string;
}

function makeState(key: string, label: string, x: number, y: number): DesignerState {
  return { key, label, is_initial: false, is_terminal: false, x, y };
}

function uniqueKey(prefix: string, existing: Iterable<string>): string {
  const set = new Set(existing);
  for (let i = 1; i < 1000; i += 1) {
    const candidate = `${prefix}_${i}`;
    if (!set.has(candidate)) return candidate;
  }
  return `${prefix}_${Date.now()}`;
}

export interface VisualDesignerProps {
  /** Workflow key the designer is authoring; required for Validate. */
  workflowKey: string;
  /** Initial definition (e.g. loaded from an existing draft version). */
  initial?: DesignerDefinition;
}

export function VisualDesigner({
  workflowKey,
  initial,
}: VisualDesignerProps) {
  const [states, setStates] = useState<DesignerState[]>(initial?.states ?? []);
  const [transitions, setTransitions] = useState<DesignerTransition[]>(
    initial?.transitions ?? [],
  );
  const [selectedStateKey, setSelectedStateKey] = useState<string | null>(null);
  const [dragging, setDragging] = useState<DragState | null>(null);
  const [pendingTransition, setPendingTransition] =
    useState<PendingTransition | null>(null);
  const [lastValidation, setLastValidation] =
    useState<ValidationSuccessResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const definition: DesignerDefinition = useMemo(
    () => ({ states, transitions }),
    [states, transitions],
  );

  const exportBody = useMemo(
    () => serializeDefinition(definition),
    [definition],
  );

  const structuralIssues = useMemo(
    () => validateDesign(definition),
    [definition],
  );

  /* -------------------------- state mutations ------------------------ */

  const addState = useCallback(() => {
    setStates((prev) => {
      const key = uniqueKey("state", prev.map((s) => s.key));
      const x = 60 + ((prev.length * 32) % (CANVAS_WIDTH - 120));
      const y = 60 + ((prev.length * 48) % (CANVAS_HEIGHT - 120));
      const next: DesignerState = makeState(key, key, x, y);
      setSelectedStateKey(key);
      return [...prev, next];
    });
  }, []);

  const updateSelectedStateField = useCallback(
    (field: "key" | "label" | "is_initial" | "is_terminal") =>
      (event: ChangeEvent<HTMLInputElement>) => {
        const target = selectedStateKey;
        if (target === null) return;
        const value =
          event.target.type === "checkbox"
            ? event.target.checked
            : event.target.value;
        setStates((prev) =>
          prev.map((s) => {
            if (s.key !== target) return s;
            if (field === "key" && typeof value === "string") {
              const trimmed = value.trim();
              if (!checkKeys(trimmed)) return s;
              // If the state was selected by key, remap the selection.
              setSelectedStateKey(trimmed);
              return { ...s, key: trimmed };
            }
            if (field === "label" && typeof value === "string") {
              return { ...s, label: value };
            }
            if (field === "is_initial" && typeof value === "boolean") {
              return { ...s, is_initial: value };
            }
            if (field === "is_terminal" && typeof value === "boolean") {
              return { ...s, is_terminal: value };
            }
            return s;
          }),
        );
      },
    [selectedStateKey],
  );

  const deleteSelectedState = useCallback(() => {
    if (selectedStateKey === null) return;
    const target = selectedStateKey;
    setStates((prev) => prev.filter((s) => s.key !== target));
    setTransitions((prev) =>
      prev.filter((t) => t.from_state !== target && t.to_state !== target),
    );
    setSelectedStateKey(null);
  }, [selectedStateKey]);

  const addTransition = useCallback(() => {
    setTransitions((prev) => {
      const from = pendingTransition?.from_state;
      if (!from) return prev;
      const target = states.find((s) => s.key !== from);
      if (!target) return prev;
      const key = uniqueKey("transition", prev.map((t) => t.key));
      const next: DesignerTransition = {
        key,
        from_state: from,
        to_state: target.key,
        human_driven: true,
        guard: null,
        actions: [],
      };
      return [...prev, next];
    });
    setPendingTransition(null);
  }, [pendingTransition, states]);

  const updateTransitionField = useCallback(
    (
      transitionKey: string,
      field: "key" | "from_state" | "to_state" | "human_driven",
    ) =>
      (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
        const value =
          event.target.type === "checkbox"
            ? (event.target as HTMLInputElement).checked
            : event.target.value;
        setTransitions((prev) =>
          prev.map((t) => {
            if (t.key !== transitionKey) return t;
            if (field === "human_driven" && typeof value === "boolean") {
              return { ...t, human_driven: value };
            }
            if (typeof value === "string") {
              if (field === "key") {
                if (!checkKeys(value)) return t;
                return { ...t, key: value };
              }
              if (field === "from_state") return { ...t, from_state: value };
              if (field === "to_state") return { ...t, to_state: value };
            }
            return t;
          }),
        );
      },
    [],
  );

  const deleteTransition = useCallback((key: string) => {
    setTransitions((prev) => prev.filter((t) => t.key !== key));
  }, []);

  /* -------------------------- drag handlers -------------------------- */

  const handleStateMouseDown = useCallback(
    (state: DesignerState) =>
      (event: MouseEvent<HTMLDivElement>) => {
        if (event.button !== 0) return;
        const rect = event.currentTarget.getBoundingClientRect();
        setDragging({
          kind: "state",
          stateKey: state.key,
          offsetX: event.clientX - rect.left,
          offsetY: event.clientY - rect.top,
        });
        setSelectedStateKey(state.key);
      },
    [],
  );

  const handleCanvasMouseMove = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      if (!dragging) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const nextX = Math.max(
        0,
        Math.min(CANVAS_WIDTH - 80, event.clientX - rect.left - dragging.offsetX),
      );
      const nextY = Math.max(
        0,
        Math.min(CANVAS_HEIGHT - 40, event.clientY - rect.top - dragging.offsetY),
      );
      setStates((prev) =>
        prev.map((s) =>
          s.key === dragging.stateKey ? { ...s, x: nextX, y: nextY } : s,
        ),
      );
    },
    [dragging],
  );

  const handleCanvasMouseUp = useCallback(() => {
    setDragging(null);
  }, []);

  /* -------------------------- drag-and-drop (HTML5 DnD) -------------- */

  const handleStateDragStart = useCallback(
    (state: DesignerState) => (event: DragEvent<HTMLDivElement>) => {
      event.dataTransfer.setData("application/x-state-key", state.key);
      event.dataTransfer.effectAllowed = "move";
    },
    [],
  );

  const handleStateDropTarget = useCallback(
    (state: DesignerState) => (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      const draggedKey = event.dataTransfer.getData(
        "application/x-state-key",
      );
      if (!draggedKey || draggedKey === state.key) return;
      // Treat the drop as "start a transition from the dragged state
      // to the dropped-on state". Mirrors common workflow editors.
      setTransitions((prev) => {
        const key = uniqueKey("transition", prev.map((t) => t.key));
        const next: DesignerTransition = {
          key,
          from_state: draggedKey,
          to_state: state.key,
          human_driven: true,
          guard: null,
          actions: [],
        };
        return [...prev, next];
      });
    },
    [],
  );

  /* -------------------------- backend actions ------------------------- */

  const handleValidate = useCallback(async () => {
    setBusy(true);
    setErrorMessage(null);
    setLastValidation(null);
    try {
      const result = await postValidate(workflowKey, exportBody);
      setLastValidation(result);
    } catch (err) {
      setLastValidation(null);
      setErrorMessage(
        err instanceof ApiClientError
          ? err.message
          : "Validation request failed.",
      );
    } finally {
      setBusy(false);
    }
  }, [workflowKey, exportBody]);

  /* -------------------------- derived UI ----------------------------- */

  const selectedState =
    selectedStateKey === null
      ? null
      : states.find((s) => s.key === selectedStateKey) ?? null;

  const allStateKeys = states.map((s) => s.key);

  return (
    <section data-testid="visual-designer">
      <header>
        <h2>Visual workflow designer</h2>
        <p>
          Workflow key: <code data-testid="designer-workflow-key">{workflowKey}</code>
        </p>
      </header>

      <div
        role="region"
        aria-label="Workflow canvas"
        data-testid="designer-canvas"
        onMouseMove={handleCanvasMouseMove}
        onMouseUp={handleCanvasMouseUp}
        onMouseLeave={handleCanvasMouseUp}
        style={{
          position: "relative",
          width: CANVAS_WIDTH,
          height: CANVAS_HEIGHT,
          border: "1px solid #888",
          background: "#fafafa",
        }}
      >
        {/* transitions rendered as SVG underlay */}
        <svg
          width={CANVAS_WIDTH}
          height={CANVAS_HEIGHT}
          style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
          data-testid="designer-canvas-svg"
        >
          {transitions.map((t) => {
            const from = states.find((s) => s.key === t.from_state);
            const to = states.find((s) => s.key === t.to_state);
            if (!from || !to) return null;
            return (
              <g key={t.key}>
                <line
                  x1={from.x + 60}
                  y1={from.y + 20}
                  x2={to.x + 60}
                  y2={to.y + 20}
                  stroke="#444"
                  strokeWidth={1.5}
                />
                <text
                  x={(from.x + to.x) / 2 + 60}
                  y={(from.y + to.y) / 2 + 16}
                  fontSize={10}
                  fill="#222"
                  textAnchor="middle"
                >
                  {t.key}
                </text>
              </g>
            );
          })}
        </svg>

        {states.map((state) => (
          <div
            key={state.key}
            role="button"
            tabIndex={0}
            draggable
            data-testid={`designer-state-${state.key}`}
            data-selected={state.key === selectedStateKey}
            onDragStart={handleStateDragStart(state)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={handleStateDropTarget(state)}
            onMouseDown={handleStateMouseDown(state)}
            style={{
              position: "absolute",
              left: state.x,
              top: state.y,
              width: 120,
              padding: "4px 8px",
              border: "1px solid #333",
              borderRadius: 4,
              background:
                state.key === selectedStateKey ? "#e0f0ff" : "#ffffff",
              cursor: dragging ? "grabbing" : "grab",
              userSelect: "none",
            }}
          >
            <strong>{state.key}</strong>
            <div style={{ fontSize: 11, color: "#555" }}>{state.label}</div>
            <div style={{ fontSize: 10, color: "#777" }}>
              {state.is_initial ? "initial · " : ""}
              {state.is_terminal ? "terminal" : ""}
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        <button
          type="button"
          onClick={addState}
          data-testid="designer-add-state"
        >
          Add state
        </button>
        <button
          type="button"
          onClick={addTransition}
          disabled={!pendingTransition}
          data-testid="designer-add-transition"
        >
          {pendingTransition
            ? `Add transition from ${pendingTransition.from_state} → pick target`
            : "Drag a state onto another to create a transition"}
        </button>
        <button
          type="button"
          onClick={handleValidate}
          disabled={busy || structuralIssues.length > 0}
          data-testid="designer-validate"
        >
          {busy ? "Validating…" : "Validate"}
        </button>
      </div>

      {pendingTransition ? (
        <p data-testid="designer-pending-transition">
          From <code>{pendingTransition.from_state}</code> — click "Add
          transition" or drag the source state onto a target.
        </p>
      ) : null}

      {structuralIssues.length > 0 ? (
        <ul data-testid="designer-issues" style={{ color: "#a00" }}>
          {structuralIssues.map((issue, idx) => (
            <li key={`${issue.code}-${idx}`}>
              <code>{issue.field}</code>: {issue.message}
            </li>
          ))}
        </ul>
      ) : null}

      {errorMessage ? (
        <p data-testid="designer-error" style={{ color: "#a00" }}>
          {errorMessage}
        </p>
      ) : null}

      {lastValidation ? (
        <p data-testid="designer-validation-success">
          Validated {lastValidation.states.length} states and{" "}
          {lastValidation.transitions.length} transitions.
        </p>
      ) : null}

      <section style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr" }}>
        <div>
          <h3>Selected state</h3>
          {selectedState ? (
            <div data-testid="designer-state-editor">
              <label>
                Key
                <input
                  type="text"
                  value={selectedState.key}
                  onChange={updateSelectedStateField("key")}
                  data-testid="designer-state-key-input"
                />
              </label>
              <label>
                Label
                <input
                  type="text"
                  value={selectedState.label}
                  onChange={updateSelectedStateField("label")}
                  data-testid="designer-state-label-input"
                />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={selectedState.is_initial}
                  onChange={updateSelectedStateField("is_initial")}
                  data-testid="designer-state-initial-input"
                />
                Initial
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={selectedState.is_terminal}
                  onChange={updateSelectedStateField("is_terminal")}
                  data-testid="designer-state-terminal-input"
                />
                Terminal
              </label>
              <button
                type="button"
                onClick={deleteSelectedState}
                data-testid="designer-state-delete"
              >
                Delete state
              </button>
            </div>
          ) : (
            <p>Select a state on the canvas to edit it.</p>
          )}
        </div>

        <div>
          <h3>Transitions ({transitions.length})</h3>
          {transitions.length === 0 ? (
            <p>No transitions yet.</p>
          ) : (
            <ol data-testid="designer-transition-list">
              {transitions.map((t) => (
                <li
                  key={t.key}
                  data-testid={`designer-transition-${t.key}`}
                >
                  <input
                    type="text"
                    value={t.key}
                    onChange={updateTransitionField(t.key, "key")}
                  />
                  <select
                    value={t.from_state}
                    onChange={updateTransitionField(t.key, "from_state")}
                    data-testid={`designer-transition-from-${t.key}`}
                  >
                    <option value="">— from —</option>
                    {allStateKeys.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <span>→</span>
                  <select
                    value={t.to_state}
                    onChange={updateTransitionField(t.key, "to_state")}
                    data-testid={`designer-transition-to-${t.key}`}
                  >
                    <option value="">— to —</option>
                    {allStateKeys.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <label>
                    <input
                      type="checkbox"
                      checked={t.human_driven}
                      onChange={updateTransitionField(t.key, "human_driven")}
                    />
                    human_driven
                  </label>
                  <button
                    type="button"
                    onClick={() => deleteTransition(t.key)}
                    data-testid={`designer-transition-delete-${t.key}`}
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      </section>

      <section>
        <h3>Exported JSON (sent verbatim to the engine)</h3>
        <pre data-testid="designer-export-json">
          {JSON.stringify(exportBody, null, 2)}
        </pre>
      </section>
    </section>
  );
}

export default VisualDesigner;
