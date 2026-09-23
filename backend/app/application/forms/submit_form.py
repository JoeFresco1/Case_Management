"""Submit a published form for a case (P07 / FM-036).

The submit-form use case is the canonical atomic form-submission flow:

1. **Authorize** the actor against ``Action.CASE_UPDATE`` on the target
   case (P11-US2 / FM-002). Forms are data-collection surfaces that
   write to the case; they share the same authorization surface as
   ``PATCH /cases/{id}`` rather than introducing a separate grant.
2. **Resolve the published binding** (P07-US4). The form key resolves
   to a ``FormBinding`` whose ``case_type_version_id`` must equal the
   case's pinned ``case_type_version_id`` — the binding is the
   immutable contract that locks the form lineage to a case-type
   lineage (acceptance scenario G4).
3. **Resolve the published form version**. A draft or retired form
   is rejected with ``ValidationFailed`` ("draft form not submittable"
   / "retired form not submittable" — explicit acceptance criterion).
4. **Evaluate section visibility** (P07-US2 / US3). For each section
   we evaluate the ``visible_when`` condition against a merged
   context: the case's stored data **minus** any sibling field values
   that are hidden by a different section in this form, plus any
   submission overrides for keys the visibility rules actually
   reference. The set of hidden field keys is collected.
5. **Apply hidden-field preservation** (P07-US3). Submitted data for
   hidden fields is dropped; previously stored values win.
6. **Validate visible required fields + form-level rules**
   (P07 acceptance scenario G2). Required fields in hidden sections
   are not enforced — a user never sees them — but form-level
   validations (``when`` -> ``then: required_field``) are evaluated
   against the *visible* merged data.
7. **Edit the case atomically** by delegating to the same primitives
   ``edit_case`` uses (case + audit + outbox, ``If-Match`` token,
   optimistic concurrency). The repository UPDATE, the audit row,
   and the outbox row share one transaction; the boundary commits
   once.

# INTEGRATION_SEAM

The use case depends only on domain ports (``CaseRepository``,
``CaseTypeVersionRepository``, audit / outbox writers). The SQL
adapter is wired in the HTTP router (``interfaces/http/routers/forms.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.compute_derived import compute_derived_fields
from app.application.forms.intake_submission import (
    apply_calculated_fields,
    apply_lookup_prepopulation,
    check_signature_fields,
    hidden_field_keys,
    validate_visible_fields_deterministic,
)
from app.domain.audit import CASE_UPDATED
from app.domain.case import Case, FieldDiff, FieldSnapshot
from app.domain.errors import (
    Forbidden,
    NotFound,
    ValidationFailed,
)
from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormVersion,
    FormVersionStatus,
    _evaluate_visibility,
    apply_submission_preservation,
    read_only_field_keys,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    OutboxWriter,
)

SUBJECT_TYPE_CASE = "case"


# ---------------------------------------------------------------------------
# Ports (Protocol-typed so the use case is unit-testable with fakes)
# ---------------------------------------------------------------------------


class FormVersionLoader(Protocol):
    """Single-form-version lookup seam."""

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None: ...


class FormBindingRepository(Protocol):
    """Latest-published binding lookup seam.

    The repository returns the *published* binding for a given
    (tenant_id, form_key). Unpublished bindings are intentionally
    invisible to the submit path: a process administrator must
    publish a binding before intake users can submit (P07-US4).
    """

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None: ...


class _ClockLike(Protocol):
    """Minimal clock protocol. The application layer must not import
    ``app.deps.Clock`` (that would pull FastAPI in); a duck-typed
    fixed clock satisfies this shape in tests."""

    def now(self) -> Any: ...


# ---------------------------------------------------------------------------
# Commands and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmitFormCommand:
    """Input to :func:`handle`.

    The caller specifies the *form key* (a tenant-local slug); the
    use case resolves the latest published binding and form version.
    ``expected_version_token`` is the ``If-Match`` header at the HTTP
    boundary (AGENTS.md §10) — the same optimistic concurrency
    guarantee as ``PATCH /cases/{id}``.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    form_key: str
    submitted_data: Mapping[str, Any]
    expected_version_token: int
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitFormResult:
    """The post-commit outcome.

    ``case`` is the updated aggregate so the router can build a
    fresh ``CaseSummary``. ``hidden_keys`` is the set of field keys
    that were hidden by section visibility at submission time — the
    caller can surface this for auditability / observability.
    """

    case: Case
    diff: FieldDiff
    hidden_keys: frozenset[str]


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


async def handle(
    cmd: SubmitFormCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    form_binding_repo: FormBindingRepository,
    form_version_loader: FormVersionLoader,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    lookup: Any | None = None,
    signature_connector: Any | None = None,
    document_repo: Any | None = None,
) -> SubmitFormResult:
    """Submit a published form against a case (P07 / FM-036).

    Failure behavior:
    - Auth / validation / not-found errors are raised before any DB write.
      The caller retains the submitted payload so the UI re-renders
      entered values with per-field highlights (``ValidationFailed.field``
      names every offender; nothing was written).
    - ``Conflict`` (stale ``version_token``) is raised by the
      aggregate before any audit or outbox row is staged.
    - The case UPDATE, audit row, and outbox row share one transaction;
      the caller commits once. If the audit or outbox write fails the
      case UPDATE is rolled back.

    Deterministic field pipeline (shared with intake submission): hidden
    + read-only/system/calculated inbound values are dropped, blank
    lookups are pre-populated, calculated formulas are re-evaluated, the
    signature seam is enforced, and visible required + per-field
    type/range/pattern/enum + form-level rules are collected into one
    field-named error. Attachment ``{"document_id": ...}`` references are
    verified against ``document_repo`` when supplied.

    Raises:
        Forbidden: actor lacks ``CASE_UPDATE`` on the case.
        NotFound: case, case-type version, binding, or form version missing.
        ValidationFailed: form key has no published binding, the form
            version is not ``published``, the bound case-type version
            does not match the case's pinned version, or deterministic
            field / attachment / signature validation fails.
        DependencyUnavailable: lookup source or signature connector
            unreachable (caller rolls back; nothing was staged).
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_UPDATE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    # 1. Resolve the case + pinned case-type version.
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")
    case_type_version = await case_type_version_repo.get(cmd.tenant_id, case.case_type_version_id)
    if case_type_version is None:
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    # 2. Resolve the published binding + form version.
    binding = await form_binding_repo.get_published_for_key(cmd.tenant_id, cmd.form_key)
    if binding is None:
        raise ValidationFailed(f"no published form binding for {cmd.form_key!r}")
    if binding.case_type_version_id != case.case_type_version_id:
        raise ValidationFailed("form binding does not match the case's case-type version")
    form_version = await form_version_loader.get_by_id(cmd.tenant_id, binding.form_version_id)
    if form_version is None:
        raise NotFound(f"form version {binding.form_version_id} not found")
    if form_version.status is not FormVersionStatus.PUBLISHED:
        raise ValidationFailed(
            f"form {form_version.key!r} is not published (status={form_version.status.value})"
        )
    if form_version.tenant_id != cmd.tenant_id:
        raise Forbidden("form belongs to a different tenant")

    # 3. Evaluate section visibility + hidden keys.
    definition = form_version.definition
    visible_sections = form_version.visible_section_keys(
        visible_data=case.data,
        submission_data=cmd.submitted_data,
    )
    hidden_keys = hidden_field_keys(definition, visible_sections)

    # 4. Merge submitted data over stored data, preserving hidden fields
    # (P07-US3) and dropping read-only/system/calculated inbound values
    # (platform-owned; caller values never win).
    merged = apply_submission_preservation(
        stored_data=case.data,
        submitted_data=cmd.submitted_data,
        hidden_keys=hidden_keys,
        read_only_keys=read_only_field_keys(definition),
    )

    # 4a. Lookup pre-population (blank lookups filled deterministically;
    # dependency failure propagates for rollback).
    merged = await apply_lookup_prepopulation(
        definition, merged, tenant_id=cmd.tenant_id, lookup=lookup
    )

    # 4b. Calculated fields (formula-owned values, dependency-ordered).
    merged, _calculated_keys = apply_calculated_fields(definition, merged)

    # 4c. Signature seam (external kinds fail closed without a connector).
    await check_signature_fields(
        definition, merged, tenant_id=cmd.tenant_id, connector=signature_connector
    )

    # 4d. Attachment reference routing: pre-uploaded document ids must
    # resolve in-tenant (never an existence oracle across tenants).
    merged = await _verify_attachment_references(
        definition, merged, tenant_id=cmd.tenant_id, document_repo=document_repo
    )

    # 5. Deterministic validation: visible required + per-field
    # type/range/pattern/enum + form-level rules, all offenders named in
    # one ``ValidationFailed`` before any DB write (no data loss).
    validate_visible_fields_deterministic(
        definition=definition,
        visible_sections=visible_sections,
        merged_data=merged,
    )

    # 6. Delegate to the aggregate's edit primitive so the version
    # token, audit, and outbox flow match ``PATCH /cases/{id}``.
    now = clock.now()
    new_case = case.edit(
        expected_version_token=cmd.expected_version_token,
        data=merged,
        case_type_version=case_type_version,
        now=now,
    )
    if new_case.version_token == case.version_token:
        return SubmitFormResult(
            case=new_case,
            diff=FieldDiff(),
            hidden_keys=hidden_keys,
        )

    final_data = compute_derived_fields(dict(new_case.data), case_type_version.schema)
    if final_data != new_case.data:
        new_case = replace(new_case, data=final_data)

    diff = FieldDiff(
        before=_snapshot_of(case),
        after=_snapshot_of(new_case),
        changed_fields=_changed_fields(case, new_case),
    )

    await case_repo.update(new_case)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_UPDATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=new_case.id,
        case_id=new_case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=cmd.causation_id,
        payload={
            "reference": new_case.reference.value,
            "version_token": new_case.version_token,
            "previous_version_token": case.version_token,
            "form_key": form_version.key,
            "form_version_id": str(form_version.id),
            "form_version_number": form_version.version_number,
            "hidden_keys": sorted(hidden_keys),
            "field_diff": diff.to_payload(),
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_UPDATED,
        event_id=uuid4(),
        payload={
            "case_id": str(new_case.id),
            "reference": new_case.reference.value,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "version_token": new_case.version_token,
            "previous_version_token": case.version_token,
            "form_key": form_version.key,
            "form_version_id": str(form_version.id),
            "hidden_keys": sorted(hidden_keys),
            "occurred_at": now.isoformat(),
        },
    )

    return SubmitFormResult(
        case=new_case,
        diff=diff,
        hidden_keys=hidden_keys,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _verify_attachment_references(
    definition: FormDefinition,
    merged_data: Mapping[str, Any],
    *,
    tenant_id: Any,
    document_repo: Any | None,
) -> dict[str, Any]:
    """Verify attachment ``{"document_id": ...}`` refs resolve in-tenant.

    Pre-uploaded documents (stored via ``upload_document`` before the
    form submit) are referenced by id. When ``document_repo`` is
    supplied, each non-blank attachment value must parse as a UUID and
    resolve via ``document_repo.get(tenant_id, doc_id)``; unknown or
    cross-tenant ids fail closed with ``ValidationFailed`` naming the
    field (never an existence oracle). With no repo wired the shape is
    still enforced (mapping with a non-blank ``document_id``).
    """
    from uuid import UUID as _UUID  # noqa: PLC0415 -- deferred to keep header lean.

    result: dict[str, Any] = dict(merged_data)
    for section in definition.sections:
        for spec in section.fields:
            if spec.field_type != "attachment":
                continue
            value = result.get(spec.key)
            if value is None or (isinstance(value, str) and value == ""):
                continue
            if not isinstance(value, Mapping) or not str(value.get("document_id") or "").strip():
                raise ValidationFailed(
                    spec.validation_message
                    or f"attachment {spec.key!r} needs an uploaded document",
                    field=spec.key,
                )
            if document_repo is None:
                continue
            try:
                doc_id = _UUID(str(value.get("document_id")))
            except ValueError as exc:
                raise ValidationFailed(
                    spec.validation_message
                    or f"attachment {spec.key!r} needs an uploaded document",
                    field=spec.key,
                ) from exc
            existing = await document_repo.get(tenant_id, doc_id)
            if existing is None:
                raise ValidationFailed(
                    f"attachment {spec.key!r} references an unknown document",
                    field=spec.key,
                )
    return result


def _hidden_field_keys(
    definition: FormDefinition,
    visible_sections: frozenset[str],
) -> frozenset[str]:
    """Return every field key that lives in a hidden section.

    Kept for backward compatibility; new code prefers
    :func:`app.application.forms.intake_submission.hidden_field_keys`
    (identical semantics).
    """
    return hidden_field_keys(definition, visible_sections)


def _validate_visible_required(
    *,
    definition: FormDefinition,
    visible_sections: frozenset[str],
    merged_data: Mapping[str, Any],
) -> None:
    """Reject if any *visible* required field is missing.

    Required fields in hidden sections are intentionally skipped —
    a user never sees them, so a missing value is not an error. The
    merged data is used (not the submission) so hidden-field
    preservation does not cause spurious validation failures.
    """
    missing: list[str] = []
    for section in definition.sections:
        if section.key not in visible_sections:
            continue
        for field in section.fields:
            if not field.required:
                continue
            if _is_blank(merged_data.get(field.key)):
                missing.append(field.key)
    if missing:
        raise ValidationFailed(
            "required fields missing on form submission: " + ", ".join(sorted(missing)),
            field=",".join(sorted(missing)),
        )


def _validate_form_rules(
    *,
    definition: FormDefinition,
    merged_data: Mapping[str, Any],
) -> None:
    """Evaluate the form's ``validations`` rules.

    Each rule has shape ``{"when": <condition>, "then": {"required":
    "<field>"}}``. If the ``when`` condition holds and the named
    field is blank, reject with ``ValidationFailed`` so the operator
    gets a structured 422 (P07 acceptance scenario G2).
    """
    for rule in definition.validations:
        when = rule.get("when") if isinstance(rule, Mapping) else None
        if when is None:
            continue
        if not _evaluate_visibility(when, merged_data):
            continue
        then = rule.get("then") if isinstance(rule, Mapping) else None
        if not isinstance(then, Mapping):
            continue
        required_field = then.get("required")
        if not isinstance(required_field, str):
            continue
        if _is_blank(merged_data.get(required_field)):
            raise ValidationFailed(
                f"form rule requires {required_field!r} when the condition holds",
                field=required_field,
            )


def _is_blank(value: Any) -> bool:
    """Treat ``None``, empty string, and empty containers as blank.

    ``0`` and ``False`` are **not** blank — a deliberate numeric /
    boolean value should pass required-field validation.
    """
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    return bool(isinstance(value, (list, tuple, dict, set)) and len(value) == 0)


def _snapshot_of(case: Case) -> FieldSnapshot:
    """Capture editable fields for the audit diff."""
    return FieldSnapshot(
        title=case.title,
        description=case.description,
        priority=case.priority,
        data=dict(case.data),
    )


def _changed_fields(before: Case, after: Case) -> frozenset[str]:
    """Return the set of editable fields that differ between two cases."""
    changed: set[str] = set()
    if before.title != after.title:
        changed.add("title")
    if before.description != after.description:
        changed.add("description")
    if before.priority != after.priority:
        changed.add("priority")
    if before.data != after.data:
        changed.add("data")
    return frozenset(changed)


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "FormVersionLoader",
    "FormBindingRepository",
    "SubmitFormCommand",
    "SubmitFormResult",
    "handle",
]
