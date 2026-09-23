import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiClientError,
  isForbiddenLike,
  type FormVersionResponse,
  type SubmitFormResponse,
} from "../api/client";
import {
  FormDefinition,
  findMissingRequiredFields,
  findViolatedValidationRules,
  getHiddenFieldKeys,
  getVisibleSectionKeys,
} from "./formValidation";

/** Single form-rendering error with a stable field identity for a11y linking. */
export interface FormFieldError {
  field: string;
  message: string;
}

interface UseFormOptions {
  caseId: string;
  formKey: string;
  initialData?: Record<string, unknown>;
  onSubmit?: (response: SubmitFormResponse) => void;
}

/**
 * Normalize the generated wire definition into the shared local shape.
 *
 * Why this exists (not a second format): the OpenAPI schema marks
 * ``sections``/``validations``/``fields`` optional, while the shared
 * frontend validator (`formValidation.ts`, mirroring
 * ``backend/app/domain/form.py``) requires them. This adapter only fills
 * defaults — it never renames keys or converts formats — so the visual
 * designer output, the API payload, and the renderer input stay one shape.
 */
export function toLocalDefinition(
  wire: FormVersionResponse["definition"],
): FormDefinition {
  return {
    sections: (wire.sections ?? []).map((section) => ({
      key: section.key,
      label: section.label,
      fields: (section.fields ?? []).map((field) => ({
        key: field.key,
        label: field.label,
        required: field.required,
        placeholder: field.placeholder ?? undefined,
        help_text: field.help_text ?? undefined,
      })),
      visible_when:
        (section.visible_when as FormDefinition["sections"][number]["visible_when"]) ??
        null,
    })),
    validations: ((wire.validations ?? []) as unknown as FormDefinition["validations"]),
  };
}

/** Deterministic ordering: section order, then field order, then rule order. */
function sortFieldErrors(errors: FormFieldError[]): FormFieldError[] {
  return [...errors].sort((a, b) =>
    a.field.localeCompare(b.field) || a.message.localeCompare(b.message),
  );
}

function fieldErrorId(caseId: string, field: string): string {
  return `${caseId}-${field}-error`;
}

function serverErrorMessage(error: unknown): string {
  if (error instanceof ApiClientError) {
    if (error.status === 409) return "Someone else saved this case first. Reload and try again.";
    if (error.status === 422) return error.message || "The server rejected the submission.";
    if (error.status === 403 || error.status === 404)
      return "You do not have access to this form.";
    return error.message || "Submission failed.";
  }
  return error instanceof Error ? error.message : "Submission failed.";
}

export function FormRenderer({
  caseId,
  formKey,
  initialData = {},
  onSubmit,
}: UseFormOptions) {
  const queryClient = useQueryClient();
  const [formData, setFormData] = useState<Record<string, unknown>>(initialData);
  const [fieldErrors, setFieldErrors] = useState<FormFieldError[]>([]);
  const [generalError, setGeneralError] = useState<string | null>(null);
  const initializedFor = useRef<string | null>(null);
  const errorSummaryRef = useRef<HTMLDivElement>(null);

  const formQuery = useQuery({
    queryKey: ["form", formKey],
    queryFn: () => api.getFormVersion(formKey),
    enabled: !!formKey,
    retry: false,
  });
  const wireVersion: FormVersionResponse | undefined = formQuery.data;
  const definition: FormDefinition | null = useMemo(
    () => (wireVersion ? toLocalDefinition(wireVersion.definition) : null),
    [wireVersion],
  );

  // why: initialize once per published version so typing is never wiped by
  // a refetch or by the default `{}` identity changing each render. Later
  // definition loads only add unseen keys; entered values are preserved.
  useEffect(() => {
    if (!wireVersion || !definition) return;
    if (initializedFor.current === wireVersion.id) return;
    initializedFor.current = wireVersion.id;
    setFormData((prev) => {
      const next: Record<string, unknown> = { ...initialData, ...prev };
      for (const section of definition.sections) {
        for (const field of section.fields) {
          if (!(field.key in next)) next[field.key] = "";
        }
      }
      return next;
    });
  }, [wireVersion, definition, initialData]);

  const submitMutation = useMutation({
    mutationFn: async (data: Record<string, unknown>) => {
      const caseSummary = queryClient.getQueryData<{ version_token: number }>([
        "case",
        caseId,
      ]);
      return api.submitForm(
        caseId,
        { form_key: formKey, data },
        caseSummary?.version_token ?? 1,
      );
    },
    onSuccess: (response) => {
      setFieldErrors([]);
      setGeneralError(null);
      queryClient.invalidateQueries({ queryKey: ["case", caseId] });
      onSubmit?.(response);
    },
    onError: (error: unknown) => {
      // why: entered data stays in state — only the message changes — so a
      // 422/409 never costs the worker their typing (E-6 validation
      // preservation).
      setGeneralError(serverErrorMessage(error));
      errorSummaryRef.current?.focus();
    },
  });

  const handleChange = useCallback((key: string, value: unknown) => {
    setFormData((prev) => ({ ...prev, [key]: value }));
    setFieldErrors((prev) => prev.filter((e) => e.field !== key));
    setGeneralError(null);
  }, []);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!definition) return;

    const visibleSections = getVisibleSectionKeys(definition, formData);
    const missing = findMissingRequiredFields(definition, visibleSections, formData);
    const ruleErrors = findViolatedValidationRules(definition, formData);
    const collected = sortFieldErrors([
      ...missing.map((key): FormFieldError => ({
        field: key,
        message: `Form field ${key} is required.`,
      })),
      ...ruleErrors.map((message): FormFieldError => {
        const match = /requires (\S+) when/.exec(message);
        return { field: match?.[1] ?? "form", message };
      }),
    ]);
    if (collected.length > 0) {
      // why: deterministic, field-named errors with entered data preserved.
      setFieldErrors(collected);
      setGeneralError(null);
      requestAnimationFrame(() => errorSummaryRef.current?.focus());
      return;
    }

    setFieldErrors([]);
    setGeneralError(null);
    try {
      await submitMutation.mutateAsync(formData);
    } catch {
      // why: onError above already set the deterministic message + preserved
      // data; swallowing here keeps the rejection handled for callers that
      // fire-and-forget the submit button.
    }
  }, [definition, formData, submitMutation]);

  const errorFields = useMemo(() => new Set(fieldErrors.map((e) => e.field)), [fieldErrors]);

  if (formQuery.isLoading) {
    return <div className="form-loading">Loading form…</div>;
  }

  if (formQuery.isError) {
    if (isForbiddenLike(formQuery.error)) {
      return (
        <div className="form-error-general" role="alert">
          You do not have access to this form.
        </div>
      );
    }
    const status = formQuery.error instanceof ApiClientError ? formQuery.error.status : 0;
    if (status === 404) {
      return (
        <div className="form-error-general" role="alert">
          Form {formKey} is not published.
        </div>
      );
    }
    return (
      <div className="form-error-general" role="alert">
        Could not load form {formKey}. Try again.
      </div>
    );
  }

  if (!wireVersion || !definition) {
    return <div className="form-loading">Loading form…</div>;
  }

  const visibleSections = getVisibleSectionKeys(definition, formData);
  const hiddenKeys = getHiddenFieldKeys(definition, visibleSections);
  const visibleHidden = [...hiddenKeys].filter((k) => k in formData).sort();

  return (
    <form onSubmit={handleSubmit} className="form-renderer" noValidate aria-label={`Form ${formKey}`}>
      <p className="form-version" data-testid="form-version">
        {wireVersion.key} · version {wireVersion.version_number} · published
      </p>
      {(generalError || fieldErrors.length > 0) && (
        <div
          className="form-errors"
          role="alert"
          tabIndex={-1}
          ref={errorSummaryRef}
          aria-label="Form errors"
        >
          {generalError && <p className="form-error-general">{generalError}</p>}
          {fieldErrors.length > 0 && (
            <ul>
              {fieldErrors.map((err) => (
                <li key={`${err.field}-${err.message}`}>
                  <a href={`#${caseId}-${err.field}`}>{err.message}</a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {definition.sections.map((section) => {
        if (!visibleSections.has(section.key)) return null;
        return (
          <fieldset key={section.key} className="form-section">
            <legend>{section.label}</legend>
            {section.fields.map((field) => {
              const value = formData[field.key] ?? "";
              const describedBy = [
                field.help_text ? `${caseId}-${field.key}-help` : null,
                errorFields.has(field.key) ? fieldErrorId(caseId, field.key) : null,
              ].filter(Boolean).join(" ") || undefined;
              return (
                <div key={field.key} className="form-field">
                  <label htmlFor={`${caseId}-${field.key}`}>{field.label}</label>
                  <input
                    id={`${caseId}-${field.key}`}
                    name={field.key}
                    type="text"
                    value={String(value)}
                    onChange={(e) => handleChange(field.key, e.target.value)}
                    placeholder={field.placeholder}
                    required={field.required}
                    aria-required={field.required || undefined}
                    aria-invalid={errorFields.has(field.key) || undefined}
                    aria-describedby={describedBy}
                  />
                  {field.help_text && (
                    <span className="form-help" id={`${caseId}-${field.key}-help`}>
                      {field.help_text}
                    </span>
                  )}
                  {errorFields.has(field.key) && (
                    <span
                      className="form-field-error"
                      id={fieldErrorId(caseId, field.key)}
                    >
                      {fieldErrors.find((e) => e.field === field.key)?.message}
                    </span>
                  )}
                </div>
              );
            })}
          </fieldset>
        );
      })}

      {visibleHidden.length > 0 && (
        <p className="form-field-preserved" aria-live="polite">
          {visibleHidden.length} hidden answer{visibleHidden.length === 1 ? "" : "s"} preserved: {visibleHidden.join(", ")}
        </p>
      )}

      <div className="form-actions">
        <button type="submit" disabled={submitMutation.isPending} className="btn btn-primary">
          {submitMutation.isPending ? "Submitting…" : "Submit"}
        </button>
      </div>
    </form>
  );
}

export default FormRenderer;
