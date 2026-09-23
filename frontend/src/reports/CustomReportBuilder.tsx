import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ApiClientError } from "../api/client";

import styles from "../cases/CaseWorkspace.module.css";

/**
 * Custom report designer (P12-US6, US7 / FM-053).
 *
 * The component renders a controlled builder for declarative custom
 * report specifications: a process administrator selects group-by
 * fields, aggregation kinds, optional filter clauses, and an
 * optional rolling time window. The form calls
 * ``POST /api/v1/reports/custom-definitions`` and surfaces the
 * persisted id so the operator can chain to the export endpoint.
 *
 * The component is intentionally narrow: the closed vocabulary is
 * hard-coded so the UI cannot produce a spec that the validator
 * would reject. The OpenAPI types are hand-typed at the request
 * payload level only — the response shape mirrors the wire schema
 * declared in ``backend/app/interfaces/http/schemas/reports.py``.
 *
 * Reports are read-only; the export action lives in the
 * ``OperationalReports`` view (a future slice will land the export
 * button here as well — the slice boundary keeps the builder
 * focused on the design step).
 */

const GROUP_BY_OPTIONS = [
  { value: "case.status", label: "Case status" },
  { value: "case.priority", label: "Case priority" },
  { value: "case.queue", label: "Owning queue" },
  { value: "case.workflow_state", label: "Workflow state" },
] as const;

const AGGREGATION_OPTIONS = [
  { value: "count", label: "Count" },
  { value: "overdue_sum", label: "Overdue seconds (sum)" },
] as const;

const FILTER_FIELDS = [
  { value: "case.status", label: "Case status" },
  { value: "case.priority", label: "Case priority" },
  { value: "case.queue", label: "Owning queue" },
] as const;

const FILTER_OPERATORS = [
  { value: "eq", label: "equals" },
  { value: "ne", label: "not equals" },
  { value: "in", label: "in (comma-separated)" },
] as const;

export interface CustomReportDefinition {
  id: string;
  tenant_id: string;
  key: string;
  name: string;
  description: string;
  group_by: string[];
  aggregations: string[];
  filters: Array<{
    field: string;
    op: string;
    value: string | number | string[];
  }>;
  time_window_hours: number | null;
  created_by: string;
  created_at: string;
  version_token: number;
}

export interface CustomReportBuilderProps {
  /** Optional initial key the form should pre-fill. */
  initialKey?: string;
}

interface CreateBody {
  key: string;
  name: string;
  description: string;
  group_by: string[];
  aggregations: string[];
  filters: Array<{ field: string; op: string; value: string }>;
  time_window_hours: number | null;
}

async function postDefinition(body: CreateBody): Promise<CustomReportDefinition> {
  const response = await fetch("/api/v1/reports/custom-definitions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Correlation-Id": crypto.randomUUID(),
      "X-Dev-User": "process_admin",
      "X-Dev-Tenant": "00000000-0000-0000-0000-000000000000",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail: { message?: string } = {};
    try {
      detail = await response.json();
    } catch {
      // ignore parse errors
    }
    throw new ApiClientError(
      detail.message ?? `definition failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as CustomReportDefinition;
}

async function fetchDefinitions(): Promise<CustomReportDefinition[]> {
  const response = await fetch("/api/v1/reports/custom-definitions", {
    headers: {
      "X-Correlation-Id": crypto.randomUUID(),
      "X-Dev-User": "process_admin",
      "X-Dev-Tenant": "00000000-0000-0000-0000-000000000000",
    },
  });
  if (!response.ok) {
    let body: { message?: string } = {};
    try {
      body = await response.json();
    } catch {
      // ignore
    }
    throw new ApiClientError(
      body.message ?? `definitions fetch failed: ${response.status}`,
      response.status,
    );
  }
  const data = (await response.json()) as { items: CustomReportDefinition[] };
  return data.items ?? [];
}

function MultiSelect(props: {
  label: string;
  options: ReadonlyArray<{ value: string; label: string }>;
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  return (
    <fieldset className={styles.field}>
      <legend className={styles.label}>{props.label}</legend>
      {props.options.map((option) => {
        const checked = props.selected.includes(option.value);
        return (
          <label key={option.value} className={styles.muted}>
            <input
              type="checkbox"
              checked={checked}
              onChange={(event) => {
                const next = event.target.checked
                  ? [...props.selected, option.value]
                  : props.selected.filter((value) => value !== option.value);
                props.onChange(next);
              }}
            />{" "}
            {option.label}
          </label>
        );
      })}
    </fieldset>
  );
}

function FilterRow(props: {
  index: number;
  onRemove: () => void;
  onChange: (filter: { field: string; op: string; value: string }) => void;
  value: { field: string; op: string; value: string };
}) {
  return (
    <div className={styles.field}>
      <select
        value={props.value.field}
        onChange={(event) => props.onChange({ ...props.value, field: event.target.value })}
      >
        <option value="">— field —</option>
        {FILTER_FIELDS.map((field) => (
          <option key={field.value} value={field.value}>
            {field.label}
          </option>
        ))}
      </select>
      <select
        value={props.value.op}
        onChange={(event) => props.onChange({ ...props.value, op: event.target.value })}
      >
        {FILTER_OPERATORS.map((op) => (
          <option key={op.value} value={op.value}>
            {op.label}
          </option>
        ))}
      </select>
      <input
        type="text"
        placeholder={
          props.value.op === "in"
            ? "comma-separated values"
            : "value"
        }
        value={props.value.field === "" ? "" : String(props.value.value ?? "")}
        onChange={(event) => props.onChange({ ...props.value, value: event.target.value })}
      />
      <button type="button" onClick={props.onRemove}>
        Remove
      </button>
    </div>
  );
}

export function CustomReportBuilder(props: CustomReportBuilderProps) {
  const queryClient = useQueryClient();

  const [key, setKey] = useState(props.initialKey ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [groupBy, setGroupBy] = useState<string[]>(["case.status"]);
  const [aggregations, setAggregations] = useState<string[]>(["count"]);
  const [filters, setFilters] = useState<
    Array<{ field: string; op: string; value: string }>
  >([]);
  const [timeWindow, setTimeWindow] = useState<number | "">("");

  const definitionsQuery = useQuery({
    queryKey: ["reports", "custom-definitions"],
    queryFn: fetchDefinitions,
    staleTime: 15_000,
  });

  const create = useMutation({
    mutationFn: postDefinition,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["reports", "custom-definitions"] });
    },
  });

  const body = useMemo<CreateBody | null>(() => {
    if (!key || !name) return null;
    return {
      key,
      name,
      description,
      group_by: groupBy,
      aggregations,
      filters: filters.filter((filter) => filter.field !== "" && filter.value !== ""),
      time_window_hours: timeWindow === "" ? null : Number(timeWindow),
    };
  }, [key, name, description, groupBy, aggregations, filters, timeWindow]);

  const canSubmit = body !== null && body.group_by.length > 0 && body.aggregations.length > 0;

  return (
    <section className={styles.workspace}>
      <header className={styles.header}>
        <h2>Custom report designer (P12-US7)</h2>
        <p className={styles.muted}>
          Author a controlled declarative report spec: closed-vocabulary
          groupings, aggregations, filters, and an optional rolling time
          window. Every export is itself an audit event (P12-US6).
        </p>
      </header>

      <div className={styles.field}>
        <label className={styles.label}>Key</label>
        <input
          type="text"
          value={key}
          placeholder="e.g. open-by-queue"
          onChange={(event) => setKey(event.target.value)}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label}>Name</label>
        <input
          type="text"
          value={name}
          placeholder="Display name"
          onChange={(event) => setName(event.target.value)}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label}>Description</label>
        <textarea
          value={description}
          rows={2}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      <MultiSelect
        label="Group by"
        options={GROUP_BY_OPTIONS}
        selected={groupBy}
        onChange={setGroupBy}
      />

      <MultiSelect
        label="Aggregations"
        options={AGGREGATION_OPTIONS}
        selected={aggregations}
        onChange={setAggregations}
      />

      <div className={styles.field}>
        <label className={styles.label}>Filters</label>
        {filters.map((filter, index) => (
          <FilterRow
            key={index}
            index={index}
            value={filter}
            onChange={(next) =>
              setFilters((prior) => prior.map((entry, i) => (i === index ? next : entry)))
            }
            onRemove={() =>
              setFilters((prior) => prior.filter((_, i) => i !== index))
            }
          />
        ))}
        <button
          type="button"
          onClick={() =>
            setFilters((prior) => [
              ...prior,
              { field: "", op: "eq", value: "" },
            ])
          }
        >
          Add filter
        </button>
      </div>

      <div className={styles.field}>
        <label className={styles.label}>Time window (hours, optional)</label>
        <input
          type="number"
          min={1}
          max={8760}
          value={timeWindow}
          onChange={(event) =>
            setTimeWindow(event.target.value === "" ? "" : Number(event.target.value))
          }
        />
      </div>

      <div className={styles.field}>
        <button
          type="button"
          disabled={!canSubmit || create.isPending}
          onClick={() => {
            if (body) {
              create.mutate(body);
            }
          }}
        >
          {create.isPending ? "Saving…" : "Save definition"}
        </button>
        {create.error ? (
          <p className={styles.error}>{create.error.message}</p>
        ) : null}
        {create.data ? (
          <p className={styles.muted}>
            Saved as <code>{create.data.key}</code> ({create.data.id})
          </p>
        ) : null}
      </div>

      <header className={styles.header}>
        <h3>Saved definitions</h3>
      </header>
      {definitionsQuery.isLoading ? (
        <p>Loading…</p>
      ) : definitionsQuery.error ? (
        <p className={styles.error}>{definitionsQuery.error.message}</p>
      ) : definitionsQuery.data && definitionsQuery.data.length > 0 ? (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Group by</th>
              <th>Aggregations</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {definitionsQuery.data.map((definition) => (
              <tr key={definition.id}>
                <td>
                  <code>{definition.key}</code>
                </td>
                <td>{definition.name}</td>
                <td>{definition.group_by.join(", ")}</td>
                <td>{definition.aggregations.join(", ")}</td>
                <td>{new Date(definition.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className={styles.empty}>No custom report definitions yet.</p>
      )}
    </section>
  );
}

export default CustomReportBuilder;
