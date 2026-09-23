"""Guard context construction, guard evaluation, and the state-bound form gate.

Split from the flat ``execute_transition`` module (R2: files under 600
lines). Everything here is deterministic and (apart from the two port reads
in ``_build_guard_context`` / ``_document_counts`` / ``_tasks_for_case``)
pure — no I/O in the evaluation path. ``GuardBlocked`` lives in
``_types`` so this module can raise it without a cycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from app.application.workflow.execute_transition._types import GuardBlocked
from app.domain.case import Case
from app.domain.ports import DocumentRepository
from app.domain.rules import RuleEvaluationError, evaluate
from app.domain.task import Task
from app.domain.workflow import WorkflowTransition


def _enforce_state_form_gate(
    *,
    transition: WorkflowTransition,
    requirements: Iterable[Mapping[str, Any]],
    submitted_form_keys: Iterable[str] | None,
    declared_states: Iterable[str] | None,
) -> None:
    """Reject a transition whose target state has unsubmitted required forms.

    P07-US7: ``requirements`` are the state's ``state_forms`` entries
    (``{"form_key", "state_key", "required"}`` mappings resolved from
    the published form bindings by the caller — the engine never
    queries form storage itself, keeping the gate pure). Only entries
    whose ``state_key`` equals the transition's ``to_state`` with
    ``required=True`` (the default) can block. Malformed entries
    (missing keys, wrong types) are ignored so a sloppy optional
    entry can never wedge the workflow; a requirement naming a state
    absent from ``declared_states`` (when supplied) fails closed as
    ``guard_invalid`` so misconfiguration surfaces instead of
    silently passing.

    Deterministic: ``missing`` is sorted, so retries and replays
    produce the identical message and audit payload.
    """
    to_state = transition.to_state
    submitted = frozenset(submitted_form_keys or ())
    if declared_states is not None:
        declared = frozenset(declared_states)
        for entry in requirements:
            if not isinstance(entry, Mapping):
                continue
            state_key = entry.get("state_key")
            if isinstance(state_key, str) and state_key and state_key not in declared:
                raise GuardBlocked(
                    f"state-form requirement references undeclared state {state_key!r}",
                    reason="guard_invalid",
                    details={"transition_key": transition.key, "state_key": state_key},
                )
    missing: list[str] = []
    for entry in requirements:
        if not isinstance(entry, Mapping):
            continue
        form_key = entry.get("form_key")
        state_key = entry.get("state_key")
        if not isinstance(form_key, str) or not form_key.strip():
            continue
        if not isinstance(state_key, str) or state_key != to_state:
            continue
        if entry.get("required", True) is False:
            continue
        if form_key not in submitted:
            missing.append(form_key)
    if missing:
        ordered = sorted(set(missing))
        raise GuardBlocked(
            f"transition {transition.key!r} requires submitted forms for state "
            f"{to_state!r}: " + ", ".join(ordered),
            reason="missing_required_form",
            details={
                "transition_key": transition.key,
                "to_state": to_state,
                "missing_form_keys": ordered,
            },
        )


async def _build_guard_context(
    *,
    case: Case,
    payload: dict[str, Any] | None,
    tasks: list[Task],
    document_repo: DocumentRepository | None,
    tenant_id: UUID,
) -> dict[str, Any]:
    """Build the context dict the guard evaluator consumes.

    Guards reference fields by dotted path (``case.documents``,
    ``case.documents_by_classification.<X>``,
    ``case.participants``). The ``payload`` block surfaces the caller's
    transition payload as ``payload.*`` so a guard can require e.g.
    ``payload.decision == "approved"``. ``tasks`` is exposed as
    ``case.tasks`` so a guard can require ``len(case.tasks) >= 1``
    even though the task repository is the source of truth.

    The document-requirements block is built from
    ``DocumentRepository.list_for_case`` when a repository is wired;
    when it is not, the block is an empty mapping so a guard that
    references ``case.documents_by_classification.<X>`` fails closed
    (the caller is also expected to short-circuit on missing
    ``document_repo`` before evaluating — see ``handle``).
    """
    participants_count = sum(1 for _ in getattr(case, "participants", []) or [])
    documents_total, documents_by_classification = await _document_counts(
        document_repo, tenant_id=tenant_id, case_id=case.id
    )
    return {
        "case": {
            "id": str(case.id),
            "status": case.status.value,
            "priority": case.priority,
            "tenant_id": str(case.tenant_id),
            "participants": participants_count,
            "documents": documents_total,
            "documents_by_classification": documents_by_classification,
            "tasks": len(tasks),
            "current_workflow_state": case.current_workflow_state,
            "data": dict(case.data),
            "sensitivity": case.sensitivity,
        },
        "payload": dict(payload) if payload else {},
    }


async def _document_counts(
    document_repo: DocumentRepository | None,
    *,
    tenant_id: UUID,
    case_id: UUID,
) -> tuple[int, dict[str, int]]:
    """Return ``(total, by_classification)`` for the case's documents.

    When ``document_repo`` is ``None`` the function returns an empty
    pair so the guard context still has the shape the rules engine
    expects. The ``handle`` short-circuits on this case before
    evaluating the guard so a missing repository never silently
    satisfies a document requirement.
    """
    if document_repo is None:
        return 0, {}
    documents = list(await document_repo.list_for_case(tenant_id=tenant_id, case_id=case_id))
    by_classification: dict[str, int] = {}
    for document in documents:
        by_classification[document.classification] = (
            by_classification.get(document.classification, 0) + 1
        )
    return len(documents), by_classification


def _is_document_requirements_guard(guard: Mapping[str, Any]) -> bool:
    """Return True iff the leaf guard is a per-classification document rule.

    The authoring shape is::

        {"op": "exists",
         "field": "case.documents_by_classification.<X>",
         "min": <int>,
         "classification": "<X>"}

    The validator (``validate_workflow_definition._validate_guard_shape``)
    accepts this shape at publish time so a typo'd classification
    surfaces as a 422 before the workflow can trap a case at runtime.
    The runtime evaluator here applies the same shape check before
    consulting ``domain/rules.evaluate`` so the response payload carries
    ``reason="missing_documents"`` (P03-US5 / FM-024).
    """
    if not isinstance(guard, Mapping):
        return False
    op = guard.get("op")
    field = guard.get("field")
    if op != "exists":
        return False
    if not isinstance(field, str) or not field.startswith("case.documents_by_classification."):
        return False
    classification = guard.get("classification")
    return isinstance(classification, str) and bool(classification.strip())


def _evaluate_transition_guard(
    *,
    transition: WorkflowTransition,
    guard_context: Mapping[str, Any],
    transition_key: str,
    document_repo_present: bool,
) -> bool:
    """Evaluate the transition's guard and translate failures to ``GuardBlocked``.

    Document-requirements guards are evaluated inline so the response
    surfaces ``reason="missing_documents"`` (P03-US5); every other guard
    is handed to ``domain.rules.evaluate`` and any structural defect
    becomes ``reason="guard_invalid"``. A document guard whose
    repository is missing raises ``guard_invalid`` so the runtime never
    silently advances a case that lacks the required document.
    """
    guard = transition.guard
    assert guard is not None  # caller checks
    if _guard_requires_documents(guard) and not document_repo_present:
        raise GuardBlocked(
            f"transition {transition_key!r} requires a document repository to evaluate its guard",
            reason="guard_invalid",
            field="document_repo",
            details={"transition_key": transition_key},
        )
    if _is_document_requirements_guard(guard):
        if _evaluate_document_guard(guard, guard_context):
            return True
        raise GuardBlocked(
            f"transition {transition_key!r} requires document "
            f"classification {guard.get('classification')!r}",
            reason="missing_documents",
            field=str(guard.get("field") or "case.documents_by_classification"),
            details={
                "transition_key": transition_key,
                **_guard_failure_details(guard),
            },
        )
    try:
        return evaluate(guard, guard_context)
    except RuleEvaluationError as exc:
        raise GuardBlocked(
            f"transition {transition_key!r} guard is invalid: {exc}",
            reason="guard_invalid",
            field=str(exc.field) if exc.field else None,
            details={"transition_key": transition_key},
        ) from exc


def _evaluate_document_guard(guard: Mapping[str, Any], guard_context: Mapping[str, Any]) -> bool:
    """Evaluate a per-classification document guard against the context.

    Returns ``True`` iff the resolved count for the guard's
    ``classification`` meets or exceeds the declared ``min``. Anything
    malformed (missing context block, non-int min, unknown
    classification) returns ``False`` so the caller's fail-closed path
    raises ``GuardBlocked`` — a structural defect must never silently
    satisfy a guard.
    """
    classification = guard.get("classification")
    if not isinstance(classification, str) or not classification.strip():
        return False
    try:
        min_int = int(guard.get("min", 1))
    except (TypeError, ValueError):
        return False
    case_block = guard_context.get("case")
    if not isinstance(case_block, Mapping):
        return False
    counts = case_block.get("documents_by_classification")
    if not isinstance(counts, Mapping):
        return False
    return min_int >= 1 and int(counts.get(classification, 0)) >= min_int


def _guard_requires_documents(guard: Mapping[str, Any]) -> bool:
    """True iff the guard references a document-derived field.

    The walk is shallow because the only document-derived field the
    rule evaluator supports is ``case.documents`` and
    ``case.documents_by_classification.<X>``. We inspect the leaf
    ``field`` of every clause so a composite (``all``/``any``/``not``)
    with one document clause is correctly detected.
    """
    op = guard.get("op")
    if op in {"all", "any"}:
        return any(_guard_requires_documents(child) for child in guard.get("clauses", ()) or ())
    if op == "not":
        inner = guard.get("clause")
        return isinstance(inner, Mapping) and _guard_requires_documents(inner)
    field = guard.get("field")
    if not isinstance(field, str):
        return False
    return field.startswith("case.documents")


def _guard_failure_reason(guard: dict[str, Any]) -> str:
    """Best-effort reason for the failed guard (FR-010).

    The rule evaluator returns a plain ``False``; the use case maps
    that to a stable ``reason`` field so the HTTP layer can render a
    targeted message. The mapping is intentionally simple: a guard
    that references ``documents`` or ``participants`` indicates a
    missing-state failure; anything else is a generic ``rule_unmet``.
    """
    serialized = str(guard).lower()
    if "documents_by_classification" in serialized:
        return "missing_documents"
    if "documents" in serialized:
        return "missing_documents"
    if "participants" in serialized:
        return "missing_participant"
    if "tasks" in serialized or "completed" in serialized:
        return "unresolved_task"
    if "field" in serialized or "exists" in serialized:
        return "missing_field"
    return "rule_unmet"


def _guard_failure_details(guard: dict[str, Any]) -> dict[str, Any]:
    """Safe detail block that names the unmet requirement (FR-010).

    For a document-requirements guard the detail carries the
    ``classification`` the workflow author declared and the
    ``field`` path the rules engine evaluated. The values come from
    the guard document — never from the case — so the explanation is
    safe to surface even if the actor does not have visibility into
    the case's actual documents.
    """
    op = guard.get("op")
    if op not in {"exists", "all", "any", "not"}:
        return {}
    if op in {"all", "any", "not"}:
        # For composites we surface the outermost shape only; the
        # HTTP layer renders the per-clause detail elsewhere.
        return {"operator": op}
    field = guard.get("field")
    if not isinstance(field, str) or not field.startswith("case.documents_by_classification."):
        return {"operator": op, "field": field}
    classification = guard.get("classification")
    if not isinstance(classification, str) or not classification.strip():
        return {"operator": op, "field": field}
    return {
        "operator": op,
        "field": field,
        "classification": classification.strip(),
        "min": guard.get("min", 1),
    }
