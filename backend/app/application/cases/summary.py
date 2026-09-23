"""Build the wire-shape ``CaseSummary`` from a domain ``Case``.

This is the boundary between the application use case (which returns
domain aggregates) and the HTTP layer (which serves Pydantic schemas).
It is intentionally pure: no I/O, no logging, no clock. The only inputs
are a ``Case`` and the resolved ``case_type_key``. Reused by:

- ``GET /api/v1/cases/{id}`` — full workspace view (workspace slots are
  redacted today because participants / tasks / documents / decisions /
  timeline land in later pillars).
- ``GET /api/v1/cases`` — list view (same shape; the workspace slots are
  always redacted because the list never needs them).

The redactor strips fields the actor is not authorized to see. Today
sensitive-case redaction is a no-op for all synthesized users (every
seeded role carries ``CASE_READ``); the redaction path is wired so the
P01-US4 sensitive-case policy can land without changing the HTTP
contract.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any
from uuid import UUID

from app.application.cases.redactor import redact_case_data
from app.application.cases.sla_state import SlaStateSummary
from app.application.cases.view_case import (
    CaseSummary,
    Redacted,
    _redacted,
)
from app.domain.case import Case
from app.domain.case_type import CaseTypeSchema


def build_summary(
    *,
    case: Case,
    case_type_key: str,
    sensitive: bool = False,
    schema: CaseTypeSchema | None = None,
    can_see_sensitive: bool = False,
    sla_state: SlaStateSummary | None = None,
) -> CaseSummary:
    """Assemble a ``CaseSummary`` from a domain ``Case``.

    Args:
        case: the persisted aggregate.
        case_type_key: the stable case-type key resolved by the
            router; needed for the UI breadcrumb and for clients that
            want to fetch the type's published schema separately.
        sensitive: when True, structured ``data`` is redacted (the
            actor's role does not grant the sensitive-read action).
            The current policy treats every seeded role as
            non-sensitive; this flag is wired so the P01-US4
            sensitive-case path can land without changing call sites.
        schema: the published CaseTypeVersion schema. When provided,
            field-level sensitivity drives per-key redaction
            (P11-US6). When ``None`` (e.g. on the list view), the
            coarse ``sensitive`` flag is honored as a single switch.
        can_see_sensitive: ``True`` iff the actor's role grants
            ``Action.CASE_READ_SENSITIVE``. Honored only when
            ``schema`` is provided.
        sla_state: the projection of the case's live SLA clocks
            (P02-US6 / FM-018). When ``None`` the summary uses the
            ``Redacted`` placeholder so the field is always present
            on the wire.
    """
    if sensitive:
        # Strip the structured payload but leave the case type / status
        # / reference visible — that's how the operator knows the case
        # exists. The field shape is preserved so the client renders
        # ``(restricted)`` consistently.
        data: dict[str, Any] = {"redacted": True, "reason": "sensitive"}
    elif schema is not None:
        # P11-US6 / P05-US3: per-field sensitivity from the published
        # schema. Restricted/phi fields are replaced with the
        # redaction marker unless the actor carries the
        # sensitive-read grant.
        data = redact_case_data(data=case.data, schema=schema, can_see_sensitive=can_see_sensitive)
    else:
        data = case.data

    return CaseSummary(
        id=case.id,
        tenant_id=case.tenant_id,
        reference=case.reference.value,
        case_type_key=case_type_key,
        case_type_version_id=case.case_type_version_id,
        status=case.status.value,
        priority=case.priority,
        title=case.title,
        description=case.description,
        data=data,
        current_workflow_state=case.current_workflow_state,
        version_token=case.version_token,
        created_at=case.created_at.isoformat(),
        updated_at=case.updated_at.isoformat(),
        created_by=case.created_by,
        closed_at=case.closed_at.isoformat() if case.closed_at else None,
        sensitivity=case.sensitivity,
        # Workspace slots — deferred to later pillars. The list view
        # strips them via ``to_wire``; the workspace view returns them
        # as ``Redacted`` so the UI knows the slot exists but is
        # intentionally empty today.
        assignee=_redacted("not_implemented"),
        queue=_redacted("not_implemented"),
        # P02-US6 / FM-018: the router passes the projected SLA
        # state. The list view (``summary_to_list_dict``) strips the
        # slot because the workspace view is the only consumer.
        sla_state=sla_state if sla_state is not None else _redacted("not_implemented"),
        participants=[],
        open_tasks=[],
        recent_documents=[],
        recent_decisions=[],
        timeline=[],
        legal_hold=case.legal_hold,
    )


def summary_to_dict(summary: CaseSummary) -> dict[str, Any]:
    """Serialize a ``CaseSummary`` into the wire JSON shape.

    Handles ``Redacted`` -> ``{"redacted": true, "reason": ...}`` and
    keeps UUIDs as strings (the schema's serialization layer re-emits
    the strings).

    Why not :func:`dataclasses.asdict`: ``asdict`` recursively converts
    every dataclass-typed value into a plain dict, losing the
    ``Redacted`` runtime type. ``CaseSummary`` is ``slots=True`` so
    there is no ``__dict__``; we iterate ``fields(CaseSummary)`` and
    read each attribute via ``getattr`` so the live value (still a
    ``Redacted`` instance) reaches the dispatcher.
    """
    out: dict[str, Any] = {}
    for f in fields(CaseSummary):
        value = getattr(summary, f.name)
        if isinstance(value, Redacted):
            out[f.name] = value.to_dict()
        elif isinstance(value, list):
            out[f.name] = [_coerce(item) for item in value]
        elif isinstance(value, UUID):
            out[f.name] = str(value)
        else:
            # Why `_coerce` here and not a passthrough: scalar slots can
            # carry application-layer dataclasses (notably
            # `SlaStateSummary` in `sla_state`, which only materializes
            # when the case actually has SLA clocks). Without this the
            # dataclass reaches `CaseSummary.model_validate` raw and the
            # request 500s — intake cases never had clocks until the
            # workflow-start wiring (T070) landed, which is why the
            # latent bug survived this long.
            out[f.name] = _coerce(value)
    return out


def _coerce(value: Any) -> Any:
    if isinstance(value, Redacted):
        return value.to_dict()
    if isinstance(value, SlaStateSummary):
        return value.to_dict() if hasattr(value, "to_dict") else _sla_state_to_dict(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _sla_state_to_dict(state: SlaStateSummary) -> dict[str, Any]:
    """Serialize an :class:`SlaStateSummary` to the wire shape.

    The Pydantic schema (``SlaStateView`` in
    ``interfaces.http.schemas.cases``) is the authoritative wire
    contract; this helper produces the matching dict so the
    application-layer dataclass can be serialized without importing
    Pydantic.
    """
    return {
        "state": state.state,
        "next_due_at": (state.next_due_at.isoformat() if state.next_due_at else None),
        "breached_count": state.breached_count,
        "warning_count": state.warning_count,
        "active_count": state.active_count,
        "paused_count": state.paused_count,
        "completed_count": state.completed_count,
        "clock_keys": list(state.clock_keys),
    }


def summary_to_list_dict(summary: CaseSummary) -> dict[str, Any]:
    """Strip workspace-only slots from the summary for the list view.

    The list view never returns ``assignee``, ``queue``, ``sla_state``,
    ``participants``, ``open_tasks``, ``recent_documents``,
    ``recent_decisions``, or ``timeline``. Keeping these in the list
    payload would inflate responses for no UX gain and would invite
    consumers to fetch them from the list, breaking the workspace
    routing.
    """
    full = summary_to_dict(summary)
    for slot in (
        "assignee",
        "queue",
        "sla_state",
        "participants",
        "open_tasks",
        "recent_documents",
        "recent_decisions",
        "timeline",
    ):
        full.pop(slot, None)
    return full


__all__ = ["build_summary", "summary_to_dict", "summary_to_list_dict"]
