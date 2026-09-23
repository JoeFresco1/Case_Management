import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiClientError,
  type FormSessionResponse,
  type FormStepSpec,
  type FormVersionResponse,
} from "../api/client";
import {
  FormDefinition,
  findMissingRequiredFields,
  getVisibleSectionKeys,
} from "./formValidation";
import { toLocalDefinition } from "./FormRenderer";

interface MultiStepFormProps {
  caseId: string;
  formKey: string;
  initialData?: Record<string, unknown>;
  onSubmitted?: (session: FormSessionResponse) => void;
}

type ProgressStatus = "pending" | "current" | "completed";

interface ProgressEntry {
  step_key: string;
  status: ProgressStatus;
}

/**
 * Normalize the generated ``progress: string[][]`` tuples into typed entries.
 *
 * Why: the wire shape is ``[step_key, status][]``; this adapter only names
 * the positions — it never invents a second session format. Unknown statuses
 * fall back to ``pending`` so the UI stays deterministic.
 */
export function toProgressEntries(progress: string[][]): ProgressEntry[] {
  return progress.map((tuple) => {
    const [step_key = "", status = "pending"] = tuple;
    const normalized: ProgressStatus =
      status === "completed" || status === "current" ? status : "pending";
    return { step_key, status: normalized };
  });
}

function serverStepMessage(error: unknown): string {
  if (error instanceof ApiClientError) {
    if (error.status === 409)
      return "Someone else saved this step first. Your answers are preserved — reload and try again.";
    if (error.status === 422) return error.message || "The server rejected this step.";
    if (error.status === 403 || error.status === 404)
      return "You do not have access to this form.";
    return error.message || "Failed to save step.";
  }
  return error instanceof Error ? error.message : "Failed to save step.";
}

export function MultiStepForm({
  caseId,
  formKey,
  initialData,
  onSubmitted,
}: MultiStepFormProps): React.ReactElement {
  const queryClient = useQueryClient();
  const [stepData, setStepData] = useState<Record<string, unknown>>(initialData ?? {});
  const [stepError, setStepError] = useState<string | null>(null);
  const hydratedSession = useRef<string | null>(null);
  const startAttempted = useRef(false);
  const queryKey = useMemo(() => ["form-session", caseId, formKey], [caseId, formKey]);

  const definitionQuery = useQuery({
    queryKey: ["form", formKey],
    queryFn: () => api.forms.getVersion(formKey),
    enabled: !!formKey,
    retry: false,
  });
  const wireVersion: FormVersionResponse | undefined = definitionQuery.data;
  const definition: FormDefinition | null = useMemo(
    () => (wireVersion ? toLocalDefinition(wireVersion.definition) : null),
    [wireVersion],
  );

  const resumeQuery = useQuery<FormSessionResponse | null>({
    queryKey,
    queryFn: async () => {
      try {
        return await api.forms.resumeSession(caseId, formKey);
      } catch (error) {
        // why: resume is a pure read — 404 means "no open session", not a
        // failure. Any other status propagates to the error state below.
        if (error instanceof ApiClientError && error.status === 404) return null;
        throw error;
      }
    },
    retry: false,
  });

  const startMutation = useMutation<FormSessionResponse, Error, void>({
    mutationFn: () => api.forms.startSession(caseId, formKey),
    onSuccess: (session) => {
      queryClient.setQueryData(queryKey, session);
    },
  });

  // why: start exactly once when resume proves there is no open session.
  // Guarded by refs so the mutation object identity changing each render
  // cannot cause an infinite start loop.
  useEffect(() => {
    if (resumeQuery.isLoading || resumeQuery.isError) return;
    if (resumeQuery.data !== null) return;
    if (startAttempted.current || startMutation.isPending || startMutation.isSuccess) return;
    startAttempted.current = true;
    startMutation.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeQuery.isLoading, resumeQuery.isError, resumeQuery.data]);

  const session: FormSessionResponse | null =
    (resumeQuery.data as FormSessionResponse | null) ?? startMutation.data ?? null;

  // why: the server is the source of truth for resumed progress. Merge once
  // per session id: persisted answers first, then any caller seed, then
  // never wipe unsaved typing on later refetches.
  useEffect(() => {
    if (!session) return;
    if (hydratedSession.current === session.id) return;
    hydratedSession.current = session.id;
    setStepData((prev) => ({ ...(initialData ?? {}), ...(session.step_data ?? {}), ...prev }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  const currentStep: FormStepSpec | null = useMemo(() => {
    if (!session) return null;
    if (session.current_step) return session.current_step;
    return session.steps[session.current_step_index] ?? null;
  }, [session]);

  const progress = useMemo(
    () => toProgressEntries(session?.progress ?? []),
    [session],
  );

  const stepSections = useMemo(() => {
    if (!definition || !currentStep) return [];
    const wanted = new Set(currentStep.section_keys ?? []);
    const visible = getVisibleSectionKeys(definition, stepData);
    return definition.sections.filter(
      (section) => wanted.has(section.key) && visible.has(section.key),
    );
  }, [definition, currentStep, stepData]);

  const saveMutation = useMutation<FormSessionResponse, Error, { stepKey: string }>({
    mutationFn: ({ stepKey }) => {
      if (!session) throw new Error("no active form session");
      return api.forms.saveStep(
        caseId,
        session.id,
        stepKey,
        {
          step_key: stepKey,
          data: stepData,
          expected_version_token: session.version_token,
        },
        session.version_token,
      );
    },
    onSuccess: (updated) => {
      setStepError(null);
      queryClient.setQueryData(queryKey, updated);
      if (updated.status === "submitted") onSubmitted?.(updated);
    },
    onError: (error: unknown) => {
      // why: entered step answers stay in state — only the message changes.
      setStepError(serverStepMessage(error));
    },
  });

  const handleSaveStep = useCallback(
    (stepKey: string) => {
      if (!definition) return;
      // why: client-side required check mirrors the server so the worker
      // sees a field-named error without losing typing; the server remains
      // authoritative on submit.
      const visible = getVisibleSectionKeys(definition, stepData);
      const wanted = new Set(stepSections.flatMap((s) => s.fields.map((f) => f.key)));
      const missing = findMissingRequiredFields(definition, visible, stepData).filter((k) =>
        wanted.has(k),
      );
      if (missing.length > 0) {
        setStepError(
          `Form field ${[...missing].sort()[0]} is required before continuing.`,
        );
        return;
      }
      setStepError(null);
      saveMutation.mutate({ stepKey });
    },
    [definition, stepData, stepSections, saveMutation],
  );

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    setStepData((prev) => ({ ...prev, [key]: value }));
    setStepError(null);
  }, []);

  if (definitionQuery.isLoading || resumeQuery.isLoading) {
    return <div className="multistep-form multistep-form--loading">Loading form…</div>;
  }

  if (definitionQuery.isError || resumeQuery.isError) {
    const error = definitionQuery.isError ? definitionQuery.error : resumeQuery.error;
    if (error instanceof ApiClientError && (error.status === 403 || error.status === 404)) {
      return (
        <div className="multistep-form multistep-form--denied" role="alert">
          You do not have access to this form.
        </div>
      );
    }
    return (
      <div className="multistep-form multistep-form--error" role="alert">
        Could not load form {formKey}. Try again.
      </div>
    );
  }

  if (!session) {
    if (startMutation.isError) {
      return (
        <div className="multistep-form multistep-form--error" role="alert">
          {serverStepMessage(startMutation.error)}
        </div>
      );
    }
    return <div className="multistep-form multistep-form--empty">Starting form session…</div>;
  }

  if (session.status === "submitted") {
    return (
      <div className="multistep-form multistep-form--submitted">
        <h2>Form submitted</h2>
        <p>
          Completed {session.completed_steps.length} of {session.steps.length} steps.
        </p>
      </div>
    );
  }

  return (
    <div className="multistep-form" aria-label={`Multi-step form ${formKey}`}>
      <ol className="multistep-form__progress" aria-label="Form progress">
        {progress.map((entry) => (
          <li
            key={entry.step_key}
            className={`multistep-form__step multistep-form__step--${entry.status}`}
            aria-current={entry.status === "current" ? "step" : undefined}
          >
            {entry.step_key}
          </li>
        ))}
      </ol>

      {currentStep ? (
        <section className="multistep-form__current" aria-labelledby="multistep-current-heading">
          <h2 id="multistep-current-heading">{currentStep.label}</h2>
          {stepSections.map((section) => (
            <fieldset key={section.key} className="multistep-form__section">
              <legend>{section.label}</legend>
              {section.fields.map((field) => (
                <label key={field.key} className="multistep-form__field" htmlFor={`${caseId}-${field.key}`}>
                  <span>{field.label}</span>
                  <input
                    id={`${caseId}-${field.key}`}
                    name={field.key}
                    value={typeof stepData[field.key] === "string" ? (stepData[field.key] as string) : String(stepData[field.key] ?? "")}
                    onChange={(e) => handleFieldChange(field.key, e.target.value)}
                    required={field.required}
                    aria-required={field.required || undefined}
                  />
                </label>
              ))}
            </fieldset>
          ))}
          <button
            type="button"
            onClick={() => handleSaveStep(currentStep.step_key)}
            disabled={saveMutation.isPending}
          >
            {currentStep.required ? "Save and continue" : "Skip"}
          </button>
          {stepError ? (
            <p className="multistep-form__error" role="alert">
              {stepError}
            </p>
          ) : null}
        </section>
      ) : (
        <p>All steps completed.</p>
      )}
    </div>
  );
}

export default MultiStepForm;
