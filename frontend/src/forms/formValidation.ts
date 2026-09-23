/** Shared form-definition types and validation (P07 / FM-037 / P07-US5).
 *
 * The visual form designer, the form renderer, and any other frontend
 * consumer speak **the same JSON shape** that the backend
 * ``app.domain.form._validate_definition`` accepts. There is no second
 * format. This module is the single source of truth for that shape on
 * the frontend side; it mirrors the backend's structural rules so a
 * designer output that validates here will publish without conversion.
 *
 * The constraints enforced here intentionally mirror
 * ``backend/app/domain/form.py``:
 *
 * - ``FormSectionSpec.key`` and ``FormFieldSpec.key`` are non-empty
 *   strings, unique within their scope.
 * - ``visible_when`` is an object whose single key is one of
 *   ``eq | ne | in | exists | truthy``. ``eq`` / ``ne`` / ``in`` take a
 *   ``{field, value}`` mapping; ``in`` value must be a list; ``exists``
 *   takes either a field name string or ``{field}``; ``truthy`` takes
 *   ``{field}``.
 * - Form-level validations are ``{when, then: {required: <key>}}``
 *   mappings.
 *
 * The backend is authoritative — if the two ever diverge, the backend
 * wins (Constitution I, AGENTS.md §4.R3). When the backend rules change,
 * update this file in the same commit.
 */

export type FormOperator = "eq" | "ne" | "in" | "exists" | "truthy";

export const FORM_OPERATORS: readonly FormOperator[] = [
  "eq",
  "ne",
  "in",
  "exists",
  "truthy",
] as const;

export const MAX_FORM_CONDITION_DEPTH = 6;

export interface FormFieldSpec {
  key: string;
  label: string;
  required: boolean;
  placeholder?: string;
  help_text?: string;
}

export interface FormCondition {
  // A single-key object whose key is a FormOperator. See module docstring.
  [op: string]: unknown;
}

export interface FormSectionSpec {
  key: string;
  label: string;
  fields: FormFieldSpec[];
  visible_when?: FormCondition | null;
}

export interface FormValidationRule {
  when: FormCondition;
  then: { required: string };
}

export interface FormDefinition {
  sections: FormSectionSpec[];
  validations: FormValidationRule[];
}

/** Wire shape of a published form version (matches backend
 * ``app.domain.form.FormVersion``). Defined here so the renderer can
 * type the API response without re-declaring it. */
export interface FormVersion {
  id: string;
  key: string;
  version_number: number;
  definition: FormDefinition;
  status: string;
  published_at: string | null;
  published_by: string | null;
  created_at: string;
}

export interface FormValidationIssue {
  path: string;
  message: string;
}

/* ------------------------------------------------------------------ */
/*  Pure structural validators (mirrors backend/app/domain/form.py)    */
/* ------------------------------------------------------------------ */

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function checkCondition(
  condition: unknown,
  issues: FormValidationIssue[],
  path: string,
  depth: number = 0,
): void {
  if (depth > MAX_FORM_CONDITION_DEPTH) {
    issues.push({
      path,
      message: `form condition nesting exceeds depth ${MAX_FORM_CONDITION_DEPTH}`,
    });
    return;
  }
  if (!isPlainObject(condition) || Object.keys(condition).length === 0) {
    issues.push({ path, message: "form condition must be a non-empty mapping" });
    return;
  }
  const keys = Object.keys(condition);
  if (keys.length !== 1) {
    issues.push({
      path,
      message: "form condition must declare exactly one operator",
    });
    return;
  }
  const op = keys[0];
  if (!FORM_OPERATORS.includes(op as FormOperator)) {
    issues.push({ path, message: `unknown form operator: ${String(op)}` });
    return;
  }
  const operand = (condition as Record<string, unknown>)[op];
  if (op === "eq" || op === "ne" || op === "in") {
    if (!isPlainObject(operand)) {
      issues.push({
        path: `${path}.${op}`,
        message: `operator ${op} requires a mapping operand`,
      });
      return;
    }
    const field = operand.field;
    if (!field || typeof field !== "string") {
      issues.push({
        path: `${path}.${op}.field`,
        message: `operator ${op} requires a 'field' string`,
      });
    }
    if (!("value" in operand)) {
      issues.push({
        path: `${path}.${op}.value`,
        message: `operator ${op} requires a 'value'`,
      });
    } else if (op === "in" && !Array.isArray(operand.value)) {
      issues.push({
        path: `${path}.${op}.value`,
        message: "operator 'in' requires a list/tuple value",
      });
    }
    return;
  }
  if (op === "exists") {
    if (
      typeof operand !== "string" &&
      !(isPlainObject(operand) && typeof operand.field === "string")
    ) {
      issues.push({
        path: `${path}.${op}`,
        message:
          "operator 'exists' requires a field string or {'field': 'f'} mapping",
      });
    }
    return;
  }
  if (op === "truthy") {
    if (!isPlainObject(operand) || typeof operand.field !== "string") {
      issues.push({
        path: `${path}.${op}`,
        message: "operator 'truthy' requires a {field} mapping",
      });
    }
  }
}

/** Validate a full ``FormDefinition`` shape.
 *
 * Returns the list of structural issues (empty when valid). The caller
 * is expected to render the issues to the user or short-circuit
 * publish.
 */
export function validateFormDefinition(
  definition: unknown,
): FormValidationIssue[] {
  const issues: FormValidationIssue[] = [];
  if (!isPlainObject(definition)) {
    issues.push({ path: "", message: "form definition must be an object" });
    return issues;
  }
  const sections = (definition as { sections?: unknown }).sections;
  const validations = (definition as { validations?: unknown }).validations;
  if (!Array.isArray(sections)) {
    issues.push({
      path: "sections",
      message: "form definition must include a 'sections' array",
    });
    return issues;
  }
  if (!Array.isArray(validations)) {
    issues.push({
      path: "validations",
      message: "form definition must include a 'validations' array",
    });
    return issues;
  }
  const sectionKeys = new Set<string>();
  const seenFieldKeys = new Set<string>();
  sections.forEach((rawSection, sectionIndex) => {
    const sectionPath = `sections[${sectionIndex}]`;
    if (!isPlainObject(rawSection)) {
      issues.push({ path: sectionPath, message: "section must be an object" });
      return;
    }
    const section = rawSection as Partial<FormSectionSpec>;
    if (!section.key || typeof section.key !== "string") {
      issues.push({
        path: `${sectionPath}.key`,
        message: "form section key must be non-empty",
      });
    } else if (sectionKeys.has(section.key)) {
      issues.push({
        path: `${sectionPath}.key`,
        message: `duplicate section key ${JSON.stringify(section.key)} in form definition`,
      });
    } else {
      sectionKeys.add(section.key);
    }
    if (!section.label || typeof section.label !== "string") {
      issues.push({
        path: `${sectionPath}.label`,
        message: "form section label must be non-empty",
      });
    }
    if (section.visible_when !== undefined && section.visible_when !== null) {
      checkCondition(
        section.visible_when,
        issues,
        `${sectionPath}.visible_when`,
      );
    }
    if (!Array.isArray(section.fields)) {
      issues.push({
        path: `${sectionPath}.fields`,
        message: "section.fields must be an array",
      });
      return;
    }
    (section.fields as unknown[]).forEach((rawField, fieldIndex) => {
      const fieldPath = `${sectionPath}.fields[${fieldIndex}]`;
      if (!isPlainObject(rawField)) {
        issues.push({ path: fieldPath, message: "field must be an object" });
        return;
      }
      const field = rawField as Partial<FormFieldSpec>;
      if (!field.key || typeof field.key !== "string") {
        issues.push({
          path: `${fieldPath}.key`,
          message: "form field key must be non-empty",
        });
      } else if (seenFieldKeys.has(field.key)) {
        issues.push({
          path: `${fieldPath}.key`,
          message: `duplicate field key ${JSON.stringify(field.key)} across sections`,
        });
      } else {
        seenFieldKeys.add(field.key);
      }
      if (!field.label || typeof field.label !== "string") {
        issues.push({
          path: `${fieldPath}.label`,
          message: "form field label must be non-empty",
        });
      }
      if (typeof field.required !== "boolean") {
        issues.push({
          path: `${fieldPath}.required`,
          message: "form field required must be a boolean",
        });
      }
    });
  });
  (validations as unknown[]).forEach((rawRule, ruleIndex) => {
    const rulePath = `validations[${ruleIndex}]`;
    if (!isPlainObject(rawRule)) {
      issues.push({
        path: rulePath,
        message: "form-level validation must be a mapping",
      });
      return;
    }
    const when = (rawRule as { when?: unknown }).when;
    if (when === undefined) {
      issues.push({
        path: `${rulePath}.when`,
        message: "form-level validation must include a 'when' condition",
      });
    } else {
      checkCondition(when, issues, `${rulePath}.when`);
    }
    const then = (rawRule as { then?: unknown }).then;
    if (!isPlainObject(then)) {
      issues.push({
        path: `${rulePath}.then`,
        message: "form-level validation must include a 'then' object",
      });
      return;
    }
    const required = (then as { required?: unknown }).required;
    if (!required || typeof required !== "string") {
      issues.push({
        path: `${rulePath}.then.required`,
        message: "form-level validation 'then.required' must be a field key string",
      });
    }
  });
  return issues;
}

/** Convenience: returns true when the definition has no structural issues. */
export function isValidFormDefinition(definition: unknown): boolean {
  return validateFormDefinition(definition).length === 0;
}

/* ------------------------------------------------------------------ */
/*  Visibility evaluator (mirrors backend/app/domain/form.py)          */
/* ------------------------------------------------------------------ */

function getConditionField(op: string, operand: unknown): string {
  if (op === "exists") {
    if (typeof operand === "string") return operand;
    if (isPlainObject(operand) && typeof operand.field === "string") {
      return operand.field;
    }
    return "";
  }
  if (isPlainObject(operand) && typeof operand.field === "string") {
    return operand.field;
  }
  return "";
}

export function evaluateFormCondition(
  condition: FormCondition | null | undefined,
  context: Record<string, unknown>,
): boolean {
  if (!condition) return true;
  const entries = Object.entries(condition);
  if (entries.length !== 1) return true;
  const [op, operand] = entries[0];
  if (op === "exists") {
    const field = getConditionField(op, operand);
    return field in context && context[field] !== null && context[field] !== "";
  }
  if (op === "truthy") {
    const field = getConditionField(op, operand);
    return Boolean(context[field]);
  }
  if (!isPlainObject(operand)) return false;
  const field = getConditionField(op, operand);
  const value = (operand as { value?: unknown }).value;
  const actual = context[field];
  if (op === "eq") return actual === value;
  if (op === "ne") return actual !== value;
  if (op === "in") {
    if (!Array.isArray(actual) && typeof actual !== "string") return false;
    return (actual as readonly unknown[]).includes(value);
  }
  return false;
}

function collectReferencedKeys(
  condition: FormCondition,
  out: Set<string>,
): void {
  for (const [op, operand] of Object.entries(condition)) {
    const field = getConditionField(op, operand);
    if (field) out.add(field);
    if (isPlainObject(operand)) {
      for (const value of Object.values(operand)) {
        if (isPlainObject(value)) {
          collectReferencedKeys(value as FormCondition, out);
        }
      }
    }
  }
}

export function getReferencedVisibilityKeys(
  definition: FormDefinition,
): Set<string> {
  const referenced = new Set<string>();
  for (const section of definition.sections) {
    if (section.visible_when) {
      collectReferencedKeys(section.visible_when, referenced);
    }
  }
  return referenced;
}

export function getVisibleSectionKeys(
  definition: FormDefinition,
  visibleData: Record<string, unknown>,
  submissionData: Record<string, unknown> | null = null,
): Set<string> {
  const merged: Record<string, unknown> = { ...visibleData };
  if (submissionData) {
    const referenced = getReferencedVisibilityKeys(definition);
    for (const [key, value] of Object.entries(submissionData)) {
      if (referenced.has(key)) merged[key] = value;
    }
  }
  const visible = new Set<string>();
  for (const section of definition.sections) {
    if (!section.visible_when || evaluateFormCondition(section.visible_when, merged)) {
      visible.add(section.key);
    }
  }
  return visible;
}

export function getHiddenFieldKeys(
  definition: FormDefinition,
  visibleSections: Set<string>,
): Set<string> {
  const hidden = new Set<string>();
  for (const section of definition.sections) {
    if (visibleSections.has(section.key)) continue;
    for (const field of section.fields) hidden.add(field.key);
  }
  return hidden;
}

export function findMissingRequiredFields(
  definition: FormDefinition,
  visibleSections: Set<string>,
  data: Record<string, unknown>,
): string[] {
  const missing: string[] = [];
  for (const section of definition.sections) {
    if (!visibleSections.has(section.key)) continue;
    for (const field of section.fields) {
      if (!field.required) continue;
      const value = data[field.key];
      if (value === undefined || value === null || value === "") {
        missing.push(field.key);
      }
    }
  }
  return missing;
}

export function findViolatedValidationRules(
  definition: FormDefinition,
  data: Record<string, unknown>,
): string[] {
  const violations: string[] = [];
  for (const rule of definition.validations) {
    if (!rule.when) continue;
    if (!evaluateFormCondition(rule.when, data)) continue;
    const required = rule.then?.required;
    if (!required) continue;
    const value = data[required];
    if (value === undefined || value === null || value === "") {
      violations.push(
        `form rule requires ${required} when the condition holds`,
      );
    }
  }
  return violations;
}

export function isReferencedInCondition(
  condition: FormCondition,
  fieldKey: string,
): boolean {
  const referenced = new Set<string>();
  collectReferencedKeys(condition, referenced);
  return referenced.has(fieldKey);
}
