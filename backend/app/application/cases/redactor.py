"""Field-level sensitivity redactor (P11-US6 / P05-US3 / FM-029).

The redactor strips the **values** of any case-type field whose
``sensitivity`` is at or above the restricted threshold when the
requesting actor is not authorized to see them. The keys are preserved
in the response so the client renders ``(restricted)`` for every
restricted field without leaking the value through any side channel
(presence, ordering, count, or null-vs-string distinction).

# Authorization model

The redactor is **pure**: it does not call the policy. The caller has
already evaluated ``Action.CASE_READ`` (deny-by-default) and the
optional ``Action.CASE_READ_SENSITIVE`` for restricted fields. The
redactor only enforces what the caller already decided — replace the
value with ``RedactedField`` if the actor lacks the sensitive-read
grant. This separation lets the unit tests exercise the redactor
without spinning up the policy registry and keeps the redaction logic
deterministic.

# Logging

The redactor never logs values. Callers that need to record which
fields were redacted should log the **key list** (a count of fields
whose sensitivity is restricted) and never the values themselves.
"""

from __future__ import annotations

from typing import Any

from app.application.cases.view_case import Redacted
from app.domain.case import is_restricted_sensitivity
from app.domain.case_type import CaseTypeSchema


def redact_case_data(
    *,
    data: dict[str, Any],
    schema: CaseTypeSchema,
    can_see_sensitive: bool,
) -> dict[str, Any]:
    """Return ``data`` with restricted fields replaced by ``Redacted``.

    Args:
        data: the case's structured payload.
        schema: the published CaseTypeVersion schema that declared the
            fields' sensitivity levels. The redactor reads
            ``schema.sensitive_field_keys()``; the caller must have
            already verified the schema is published and tenant-scoped.
        can_see_sensitive: ``True`` iff the actor's role grants
            ``Action.CASE_READ_SENSITIVE``. When ``True`` the values
            pass through untouched; when ``False`` every field whose
            sensitivity is at or above ``restricted`` is replaced with
            ``{"redacted": true, "reason": "sensitive"}`` whose
            ``reason`` is the stable marker from the wire schema.

    Returns:
        A new dict mirroring the input's keys and types for non-sensitive
        fields. Restricted fields are replaced with the redaction
        marker; the original value is never copied into the return.

    Failure behavior:
        The redactor never raises on a well-formed schema. Unknown keys
        (present in ``data`` but absent from ``schema``) pass through
        unchanged — schema validation upstream is responsible for
        rejecting those at write time (``Case._validate_against_schema``).
    """
    if can_see_sensitive:
        return dict(data)
    restricted_keys = schema.sensitive_field_keys()
    if not restricted_keys:
        return dict(data)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in restricted_keys and is_restricted_sensitivity(_sensitivity_for(schema, key)):
            out[key] = Redacted(reason="sensitive").to_dict()
        else:
            out[key] = value
    return out


def _sensitivity_for(schema: CaseTypeSchema, key: str) -> str:
    """Resolve a field's sensitivity level from the schema.

    Defaults to ``"internal"`` when the field is not declared (e.g.
    legacy data persisted before the schema introduced the field). The
    default is non-restricted so unknown fields never trigger redaction
    — the schema is the contract, not the data.
    """
    field = schema.field(key)
    return field.sensitivity if field is not None else "internal"


def redacted_field_count(*, data: dict[str, Any], schema: CaseTypeSchema) -> int:
    """Return the number of fields in ``data`` that WOULD be redacted.

    Exposed for observability: callers that log audit / response
    metadata record the count of redacted fields, never the values.
    """
    return sum(1 for k in data if k in schema.sensitive_field_keys())


__all__ = ["redact_case_data", "redacted_field_count"]
