import { useCallback, useMemo, useState } from "react";
import {
  FORM_OPERATORS,
  FormCondition,
  FormDefinition,
  FormFieldSpec,
  FormOperator,
  FormSectionSpec,
  FormValidationIssue,
  FormValidationRule,
  validateFormDefinition,
} from "./formValidation";

/** Visual form designer (P07 / FM-037 / P07-US5).
 *
 * The designer is **visually** interactive but produces exactly the
 * same ``FormDefinition`` JSON consumed by the runtime form renderer
 * and validated by ``backend.app.domain.form._validate_definition``.
 * There is no second format: what the designer outputs is what
 * ``POST /api/v1/admin/forms`` accepts and what
 * ``FormVersion.publish`` will immutably snapshot.
 *
 * The designer is intentionally narrow — it covers the same surface
 * the backend rule engine knows how to evaluate:
 *
 * - sections with stable keys + human labels,
 * - fields within sections (key, label, required, placeholder, help),
 * - per-section ``visible_when`` from the operator whitelist
 *   ``eq | ne | in | exists | truthy``,
 * - form-level ``validations: [{ when, then: { required } }]``.
 *
 * Anything outside that surface (custom widgets, JS, multi-page)
 * intentionally does not exist here (pillar §2 out-of-scope).
 */

interface VisualFormDesignerProps {
  /** Optional seed loaded into the editor; e.g. an existing draft. */
  initialDefinition?: FormDefinition | null;
  /** Called whenever the designer produces a new definition. */
  onChange?: (definition: FormDefinition, issues: FormValidationIssue[]) => void;
}

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

function emptySection(labelHint = "New section"): FormSectionSpec {
  return {
    key: uid("section"),
    label: labelHint,
    fields: [emptyField("title")],
    visible_when: null,
  };
}

function emptyField(labelHint = "New field"): FormFieldSpec {
  return {
    key: uid("field"),
    label: labelHint,
    required: false,
    placeholder: undefined,
    help_text: undefined,
  };
}

function emptyValidation(): FormValidationRule {
  return {
    when: { eq: { field: "", value: "" } },
    then: { required: "" },
  };
}

function ensureConditionShape(op: FormOperator, operand: unknown): FormCondition {
  switch (op) {
    case "eq":
    case "ne":
      return {
        [op]: {
          field: typeof (operand as { field?: unknown })?.field === "string"
            ? (operand as { field: string }).field
            : "",
          value: (operand as { value?: unknown })?.value ?? "",
        },
      };
    case "in":
      return {
        in: {
          field: typeof (operand as { field?: unknown })?.field === "string"
            ? (operand as { field: string }).field
            : "",
          value: Array.isArray((operand as { value?: unknown })?.value)
            ? ((operand as { value: unknown[] }).value as unknown[])
            : [],
        },
      };
    case "exists": {
      const raw = operand as string | { field?: string } | undefined;
      if (typeof raw === "string") return { exists: raw };
      return {
        exists: {
          field: typeof raw?.field === "string" ? raw.field : "",
        },
      };
    }
    case "truthy":
      return {
        truthy: {
          field: typeof (operand as { field?: unknown })?.field === "string"
            ? (operand as { field: string }).field
            : "",
        },
      };
  }
}

function readConditionOperand(condition: FormCondition): {
  op: FormOperator;
  operand: unknown;
} {
  const entries = Object.entries(condition);
  const op = (entries[0]?.[0] ?? "eq") as FormOperator;
  return { op, operand: entries[0]?.[1] };
}

export function VisualFormDesigner({
  initialDefinition,
  onChange,
}: VisualFormDesignerProps) {
  const [definition, setDefinition] = useState<FormDefinition>(
    () =>
      initialDefinition ?? {
        sections: [emptySection("Intake")],
        validations: [],
      },
  );

  const issues = useMemo(
    () => validateFormDefinition(definition),
    [definition],
  );

  const update = useCallback(
    (next: FormDefinition) => {
      setDefinition(next);
      onChange?.(next, validateFormDefinition(next));
    },
    [onChange],
  );

  const updateSection = useCallback(
    (index: number, patch: Partial<FormSectionSpec>) => {
      const sections = definition.sections.map((s, i) =>
        i === index ? { ...s, ...patch } : s,
      );
      update({ ...definition, sections });
    },
    [definition, update],
  );

  const removeSection = useCallback(
    (index: number) => {
      const sections = definition.sections.filter((_, i) => i !== index);
      update({ ...definition, sections });
    },
    [definition, update],
  );

  const addSection = useCallback(() => {
    update({ ...definition, sections: [...definition.sections, emptySection()] });
  }, [definition, update]);

  const moveSection = useCallback(
    (index: number, direction: -1 | 1) => {
      const target = index + direction;
      if (target < 0 || target >= definition.sections.length) return;
      const sections = [...definition.sections];
      const [moved] = sections.splice(index, 1);
      sections.splice(target, 0, moved);
      update({ ...definition, sections });
    },
    [definition, update],
  );

  const updateField = useCallback(
    (sectionIndex: number, fieldIndex: number, patch: Partial<FormFieldSpec>) => {
      const section = definition.sections[sectionIndex];
      const fields = section.fields.map((f, i) =>
        i === fieldIndex ? { ...f, ...patch } : f,
      );
      updateSection(sectionIndex, { fields });
    },
    [definition, updateSection],
  );

  const removeField = useCallback(
    (sectionIndex: number, fieldIndex: number) => {
      const section = definition.sections[sectionIndex];
      const fields = section.fields.filter((_, i) => i !== fieldIndex);
      updateSection(sectionIndex, { fields });
    },
    [definition, updateSection],
  );

  const addField = useCallback(
    (sectionIndex: number) => {
      const section = definition.sections[sectionIndex];
      updateSection(sectionIndex, { fields: [...section.fields, emptyField()] });
    },
    [definition, updateSection],
  );

  const updateSectionVisibility = useCallback(
    (sectionIndex: number, op: FormOperator, operand: unknown) => {
      updateSection(sectionIndex, {
        visible_when: ensureConditionShape(op, operand),
      });
    },
    [updateSection],
  );

  const clearSectionVisibility = useCallback(
    (sectionIndex: number) => {
      updateSection(sectionIndex, { visible_when: null });
    },
    [updateSection],
  );

  const updateValidation = useCallback(
    (ruleIndex: number, patch: Partial<FormValidationRule>) => {
      const validations = definition.validations.map((r, i) =>
        i === ruleIndex ? { ...r, ...patch } : r,
      );
      update({ ...definition, validations });
    },
    [definition, update],
  );

  const removeValidation = useCallback(
    (ruleIndex: number) => {
      const validations = definition.validations.filter((_, i) => i !== ruleIndex);
      update({ ...definition, validations });
    },
    [definition, update],
  );

  const addValidation = useCallback(() => {
    update({
      ...definition,
      validations: [...definition.validations, emptyValidation()],
    });
  }, [definition, update]);

  const isValid = issues.length === 0;

  return (
    <div className="visual-form-designer">
      <header className="visual-form-designer__header">
        <h3>Form designer</h3>
        <p className="visual-form-designer__hint">
          Sections, fields, and conditional visibility are authored here. The
          emitted JSON is the exact shape the runtime accepts.
        </p>
      </header>

      <ol className="visual-form-designer__sections">
        {definition.sections.map((section, sectionIndex) => (
          <li key={section.key} className="visual-form-designer__section">
            <SectionEditor
              section={section}
              fieldKeys={collectAllFieldKeys(definition)}
              onMetaChange={(patch) => updateSection(sectionIndex, patch)}
              onAddField={() => addField(sectionIndex)}
              onRemove={() => removeSection(sectionIndex)}
              onMoveUp={() => moveSection(sectionIndex, -1)}
              onMoveDown={() => moveSection(sectionIndex, 1)}
              onUpdateField={(fieldIndex, patch) =>
                updateField(sectionIndex, fieldIndex, patch)
              }
              onRemoveField={(fieldIndex) => removeField(sectionIndex, fieldIndex)}
              onChangeVisibility={(op, operand) =>
                updateSectionVisibility(sectionIndex, op, operand)
              }
              onClearVisibility={() => clearSectionVisibility(sectionIndex)}
            />
          </li>
        ))}
      </ol>

      <div className="visual-form-designer__toolbar">
        <button type="button" onClick={addSection} className="btn">
          + Add section
        </button>
      </div>

      <section className="visual-form-designer__validations">
        <h4>Form-level validations</h4>
        {definition.validations.length === 0 && (
          <p className="visual-form-designer__empty">
            No validations. Add one to require a field when a condition holds.
          </p>
        )}
        <ol>
          {definition.validations.map((rule, ruleIndex) => (
            <li key={ruleIndex}>
              <ValidationEditor
                rule={rule}
                fieldKeys={collectAllFieldKeys(definition)}
                onChange={(patch) => updateValidation(ruleIndex, patch)}
                onRemove={() => removeValidation(ruleIndex)}
              />
            </li>
          ))}
        </ol>
        <button type="button" onClick={addValidation} className="btn">
          + Add validation rule
        </button>
      </section>

      <footer className="visual-form-designer__footer">
        <div
          className={`visual-form-designer__status ${isValid ? "is-valid" : "is-invalid"}`}
          role="status"
        >
          {isValid
            ? "Definition is valid and ready to publish."
            : `${issues.length} structural issue${issues.length === 1 ? "" : "s"} to fix before publishing.`}
        </div>
        <ul className="visual-form-designer__issues">
          {issues.map((issue, i) => (
            <li key={i}>
              <code>{issue.path}</code>: {issue.message}
            </li>
          ))}
        </ul>
        <details className="visual-form-designer__json">
          <summary>JSON preview</summary>
          <pre>{JSON.stringify(definition, null, 2)}</pre>
        </details>
      </footer>
    </div>
  );
}

function collectAllFieldKeys(definition: FormDefinition): string[] {
  const keys: string[] = [];
  for (const section of definition.sections) {
    for (const field of section.fields) keys.push(field.key);
  }
  return keys;
}

interface SectionEditorProps {
  section: FormSectionSpec;
  fieldKeys: string[];
  onMetaChange: (patch: Partial<FormSectionSpec>) => void;
  onAddField: () => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onUpdateField: (index: number, patch: Partial<FormFieldSpec>) => void;
  onRemoveField: (index: number) => void;
  onChangeVisibility: (op: FormOperator, operand: unknown) => void;
  onClearVisibility: () => void;
}

function SectionEditor({
  section,
  fieldKeys,
  onMetaChange,
  onAddField,
  onRemove,
  onMoveUp,
  onMoveDown,
  onUpdateField,
  onRemoveField,
  onChangeVisibility,
  onClearVisibility,
}: SectionEditorProps) {
  const { op, operand } = section.visible_when
    ? readConditionOperand(section.visible_when)
    : { op: "eq" as FormOperator, operand: { field: "", value: "" } };
  return (
    <article className="visual-form-designer__section-card">
      <div className="visual-form-designer__section-header">
        <label>
          Key
          <input
            type="text"
            value={section.key}
            onChange={(e) => onMetaChange({ key: e.target.value })}
            data-testid="section-key"
          />
        </label>
        <label>
          Label
          <input
            type="text"
            value={section.label}
            onChange={(e) => onMetaChange({ label: e.target.value })}
          />
        </label>
        <div className="visual-form-designer__section-actions">
          <button type="button" onClick={onMoveUp}>↑</button>
          <button type="button" onClick={onMoveDown}>↓</button>
          <button type="button" onClick={onRemove} className="btn-danger">
            Remove section
          </button>
        </div>
      </div>

      <fieldset className="visual-form-designer__visibility">
        <legend>Visible when</legend>
        {section.visible_when === null ? (
          <button
            type="button"
            onClick={() => onChangeVisibility("eq", { field: "", value: "" })}
          >
            + Add visibility rule
          </button>
        ) : (
          <ConditionEditor
            operator={op}
            operand={operand}
            availableFields={fieldKeys}
            onChange={(nextOp, nextOperand) =>
              onChangeVisibility(nextOp, nextOperand)
            }
            onRemove={onClearVisibility}
          />
        )}
      </fieldset>

      <ol className="visual-form-designer__fields">
        {section.fields.map((field, fieldIndex) => (
          <li key={fieldIndex}>
            <FieldEditor
              field={field}
              onChange={(patch) => onUpdateField(fieldIndex, patch)}
              onRemove={() => onRemoveField(fieldIndex)}
            />
          </li>
        ))}
      </ol>
      <button type="button" onClick={onAddField} className="btn">
        + Add field
      </button>
    </article>
  );
}

interface FieldEditorProps {
  field: FormFieldSpec;
  onChange: (patch: Partial<FormFieldSpec>) => void;
  onRemove: () => void;
}

function FieldEditor({ field, onChange, onRemove }: FieldEditorProps) {
  return (
    <div className="visual-form-designer__field">
      <label>
        Key
        <input
          type="text"
          value={field.key}
          onChange={(e) => onChange({ key: e.target.value })}
          data-testid="field-key"
        />
      </label>
      <label>
        Label
        <input
          type="text"
          value={field.label}
          onChange={(e) => onChange({ label: e.target.value })}
        />
      </label>
      <label className="visual-form-designer__checkbox">
        <input
          type="checkbox"
          checked={field.required}
          onChange={(e) => onChange({ required: e.target.checked })}
        />
        Required
      </label>
      <label>
        Placeholder
        <input
          type="text"
          value={field.placeholder ?? ""}
          onChange={(e) =>
            onChange({ placeholder: e.target.value || undefined })
          }
        />
      </label>
      <label>
        Help text
        <input
          type="text"
          value={field.help_text ?? ""}
          onChange={(e) =>
            onChange({ help_text: e.target.value || undefined })
          }
        />
      </label>
      <button type="button" onClick={onRemove} className="btn-danger">
        Remove field
      </button>
    </div>
  );
}

interface ConditionEditorProps {
  operator: FormOperator;
  operand: unknown;
  availableFields: string[];
  onChange: (op: FormOperator, operand: unknown) => void;
  onRemove: () => void;
}

function ConditionEditor({
  operator,
  operand,
  availableFields,
  onChange,
  onRemove,
}: ConditionEditorProps) {
  const isMappingOperand =
    operator !== "exists" ||
    (typeof operand === "object" && operand !== null && !Array.isArray(operand));
  const operandRecord = isMappingOperand
    ? (operand as Record<string, unknown>) ?? {}
    : {};

  const handleOperatorChange = (nextOp: FormOperator) => {
    const rebuilt = ensureConditionShape(nextOp, operand);
    onChange(nextOp, rebuilt[nextOp]);
  };

  return (
    <div className="visual-form-designer__condition">
      <label>
        Operator
        <select
          value={operator}
          onChange={(e) => handleOperatorChange(e.target.value as FormOperator)}
        >
          {FORM_OPERATORS.map((op) => (
            <option key={op} value={op}>
              {op}
            </option>
          ))}
        </select>
      </label>

      {operator === "exists" && typeof operand === "string" ? (
        <label>
          Field
          <input
            type="text"
            list="visual-form-designer-fields"
            value={operand}
            onChange={(e) => onChange("exists", e.target.value)}
          />
        </label>
      ) : (
        <>
          <label>
            Field
            <input
              type="text"
              list="visual-form-designer-fields"
              value={(operandRecord.field as string) ?? ""}
              onChange={(e) =>
                onChange(operator, {
                  ...operandRecord,
                  field: e.target.value,
                })
              }
            />
          </label>
          {(operator === "eq" || operator === "ne") && (
            <label>
              Value
              <input
                type="text"
                value={String(operandRecord.value ?? "")}
                onChange={(e) =>
                  onChange(operator, {
                    ...operandRecord,
                    value: e.target.value,
                  })
                }
              />
            </label>
          )}
          {operator === "in" && (
            <label>
              Value (comma list)
              <input
                type="text"
                value={((operandRecord.value as unknown[]) ?? []).join(",")}
                onChange={(e) =>
                  onChange(operator, {
                    ...operandRecord,
                    value: e.target.value
                      .split(",")
                      .map((v) => v.trim())
                      .filter((v) => v.length > 0),
                  })
                }
              />
            </label>
          )}
          {operator === "truthy" && null}
        </>
      )}

      <datalist id="visual-form-designer-fields">
        {availableFields.map((key) => (
          <option key={key} value={key} />
        ))}
      </datalist>

      <button type="button" onClick={onRemove} className="btn-danger">
        Remove rule
      </button>
    </div>
  );
}

interface ValidationEditorProps {
  rule: FormValidationRule;
  fieldKeys: string[];
  onChange: (patch: Partial<FormValidationRule>) => void;
  onRemove: () => void;
}

function ValidationEditor({
  rule,
  fieldKeys,
  onChange,
  onRemove,
}: ValidationEditorProps) {
  const { op, operand } = readConditionOperand(rule.when ?? { eq: { field: "", value: "" } });
  const required = rule.then?.required ?? "";
  return (
    <div className="visual-form-designer__validation">
      <span>When</span>
      <ConditionEditor
        operator={op}
        operand={operand}
        availableFields={fieldKeys}
        onChange={(nextOp, nextOperand) =>
          onChange({ when: ensureConditionShape(nextOp, nextOperand) })
        }
        onRemove={onRemove}
      />
      <label>
        Require field
        <input
          type="text"
          list="visual-form-designer-fields"
          value={required}
          onChange={(e) => onChange({ then: { required: e.target.value } })}
        />
      </label>
    </div>
  );
}

export default VisualFormDesigner;
