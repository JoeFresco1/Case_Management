/**
 * Schema-driven case intake form (P01-US1 / FR-001).
 *
 * The caller supplies the already-resolved published CaseType schema. This
 * keeps configuration retrieval outside the form and ensures the rendered
 * controls use the same generated OpenAPI field contract as the server.
 */
import { useState } from "react";

import type { components } from "../api/types.gen";
import { api, ApiClientError } from "../api/client";
import type { CaseSummary } from "../api/types";

type CaseTypeSchema = components["schemas"]["CaseTypeSchemaRequest"];
type FieldDefinition = components["schemas"]["FieldDefinitionRequest"];
type Priority = components["schemas"]["CreateCaseRequest"]["priority"];

export interface CreateCaseFormProps {
  caseTypeKey: string;
  publishedSchema: CaseTypeSchema;
  onCreated?: (created: CaseSummary) => void;
}

type Values = Record<string, unknown>;
type Errors = Record<string, string>;

const PRIORITIES: readonly Priority[] = ["low", "normal", "high", "urgent"];

/** Validate the fields that the case aggregate validates before persistence. */
export function validateCreateCase(
  title: string,
  priority: string,
  schema: CaseTypeSchema,
  data: Values,
): Errors {
  const errors: Errors = {};
  if (!title.trim()) errors.title = "Title is required.";
  if (!PRIORITIES.includes(priority as Priority)) errors.priority = "Select a valid priority.";
  for (const field of schema.fields ?? []) {
    if (field.required && (data[field.key] === undefined || data[field.key] === null || data[field.key] === "")) {
      errors[field.key] = `${field.label} is required.`;
    }
  }
  return errors;
}

function inputType(field: FieldDefinition): string {
  if (field.type === "longtext" || field.type === "json") return "textarea";
  if (field.type === "integer" || field.type === "decimal") return "number";
  if (field.type === "datetime") return "datetime-local";
  if (field.type === "date") return "date";
  return "text";
}

function valueFor(field: FieldDefinition, value: unknown): string | number | boolean {
  if (field.type === "boolean") return value === true;
  if (field.type === "integer" || field.type === "decimal") return typeof value === "number" ? value : "";
  return typeof value === "string" ? value : "";
}

function normalizeValue(field: FieldDefinition, value: string | boolean): unknown {
  if (field.type === "boolean") return value;
  if (field.type === "integer") return value === "" ? "" : Number.parseInt(String(value), 10);
  if (field.type === "decimal") return value === "" ? "" : Number.parseFloat(String(value));
  if (field.type === "json") {
    try {
      return value === "" ? "" : JSON.parse(String(value));
    } catch {
      return value;
    }
  }
  return value;
}

/** Render and submit a new case against a published case-type schema. */
export function CreateCaseForm({ caseTypeKey, publishedSchema, onCreated }: CreateCaseFormProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");
  const [data, setData] = useState<Values>({});
  const [errors, setErrors] = useState<Errors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [created, setCreated] = useState<CaseSummary | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function updateField(field: FieldDefinition, value: string | boolean) {
    setData((previous) => ({ ...previous, [field.key]: normalizeValue(field, value) }));
    setErrors((previous) => ({ ...previous, [field.key]: "" }));
    setSubmitError(null);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors = validateCreateCase(title, priority, publishedSchema, data);
    setErrors(nextErrors);
    setSubmitError(null);
    if (Object.keys(nextErrors).length > 0) return;
    setSubmitting(true);
    try {
      const result = await api.createCase({ case_type_key: caseTypeKey, title, description, priority, data });
      setCreated(result);
      onCreated?.(result);
    } catch (error) {
      setSubmitError(error instanceof ApiClientError ? error.message : "Unable to create case.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate aria-label="Create case">
      <h1>Create case</h1>
      {submitError && <p role="alert">{submitError}</p>}
      {created && <p role="status">Case {created.reference} created.</p>}
      <label htmlFor="case-title">Title</label>
      <input id="case-title" value={title} onChange={(event) => setTitle(event.target.value)} aria-invalid={!!errors.title} />
      {errors.title && <p role="alert">{errors.title}</p>}
      <label htmlFor="case-description">Description</label>
      <textarea id="case-description" value={description} onChange={(event) => setDescription(event.target.value)} />
      <label htmlFor="case-priority">Priority</label>
      <select id="case-priority" value={priority} onChange={(event) => setPriority(event.target.value as Priority)}>
        {PRIORITIES.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
      {(publishedSchema.fields ?? []).map((field) => {
        const type = inputType(field);
        const error = errors[field.key];
        return (
          <div key={field.key}>
            <label htmlFor={`case-data-${field.key}`}>{field.label}{field.required ? " *" : ""}</label>
            {type === "textarea" ? (
              <textarea id={`case-data-${field.key}`} value={String(valueFor(field, data[field.key]))} onChange={(event) => updateField(field, event.target.value)} aria-invalid={!!error} />
            ) : field.type === "boolean" ? (
              <input id={`case-data-${field.key}`} type="checkbox" checked={valueFor(field, data[field.key]) === true} onChange={(event) => updateField(field, event.target.checked)} aria-invalid={!!error} />
            ) : field.type === "enum" || field.type === "multichoice" ? (
              <select id={`case-data-${field.key}`} value={String(valueFor(field, data[field.key]))} onChange={(event) => updateField(field, event.target.value)} aria-invalid={!!error}>
                <option value="">Select…</option>
                {(field.enum_values ?? []).map((option) => <option key={option} value={option}>{option}</option>)}
              </select>
            ) : (
              <input id={`case-data-${field.key}`} type={type} value={valueFor(field, data[field.key]) as string | number} onChange={(event) => updateField(field, event.target.value)} aria-invalid={!!error} />
            )}
            {error && <p role="alert">{error}</p>}
          </div>
        );
      })}
      <button type="submit" disabled={submitting}>{submitting ? "Creating…" : "Create case"}</button>
    </form>
  );
}

export default CreateCaseForm;
