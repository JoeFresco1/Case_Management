"""Sensitivity classification value objects and redaction helpers (FM-011).

P11-US6 + P05-US3/US7: administrators classify cases and fields as
sensitive; unauthorized responses, logs, events, and search snippets
redact values. P05-US7 adds the encryption-at-rest seam for designated
fields (see module footer).

# Design contract: classify once, redact by reference id, never by content

- :func:`classify` resolves the *declared* sensitivity of each field key
  from the published ``CaseTypeSchema`` plus the case-level default. The
  result (:class:`SensitivityClassification`) maps **key -> level**; it
  never inspects values.
- Redaction helpers replace a sensitive value with a
  :class:`SensitiveReference` marker (``{"redacted": True,
  "reason": "sensitive", "ref": "<field_key>"}``). The marker carries
  the field's identity (the ``ref``), never the content. Consumers that
  need forensics log the ``ref`` list / count, never the values.
- Helpers are **key-based, never content-based**: a public-keyed field
  whose value happens to look sensitive (e.g. ``notes`` containing an
  SSN-shaped string) passes through untouched, while a
  restricted-keyed field with an innocuous value is still redacted.
  No helper scans, matches, or infers sensitivity from a value.

# Relationship to existing redactors

- :mod:`app.application.cases.redactor` performs the same per-field
  substitution for the case-workspace wire shape. It stays as the
  application-layer entry point; this module is the domain vocabulary
  it (and logs / events / search) shares.
- :func:`app.infrastructure.telemetry.structured_logging.redact_sensitive`
  redacts log lines by key-substring list. The log/event/snippet helpers
  here accept an explicit ``sensitive_keys`` set resolved from
  :func:`classify`, so schema-declared sensitivity drives redaction even
  for keys outside the static telemetry list.

# INTEGRATION_SEAM: field-level encryption at rest (P05-US7)

Classification labels (``restricted`` / ``phi``) are the selector the
future KeyVault adapter will use to decide which ``cases.data_json``
fields are wrapped in a per-tenant data key at write time. Until that
adapter lands, values are plaintext at rest and these helpers are the
only barrier between stored values and logs / responses / events.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.case_type import CaseTypeSchema
from app.domain.errors import InvariantViolation


class SensitivityLevel(StrEnum):
    """Ordered sensitivity ladder (P05 §2).

    Ordering: ``public`` < ``internal`` < ``confidential`` <
    ``restricted`` < ``phi``. Anything at or above ``restricted``
    requires an explicit sensitive-read grant (P11-US6).
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    PHI = "phi"

    def rank(self) -> int:
        """Return the ordinal used for threshold comparisons."""
        order: tuple[SensitivityLevel, ...] = (
            SensitivityLevel.PUBLIC,
            SensitivityLevel.INTERNAL,
            SensitivityLevel.CONFIDENTIAL,
            SensitivityLevel.RESTRICTED,
            SensitivityLevel.PHI,
        )
        return order.index(self)

    def is_restricted(self) -> bool:
        """Return True when this level requires field-level authorization."""
        return self in (SensitivityLevel.RESTRICTED, SensitivityLevel.PHI)


# why: single threshold constant so domain, application, and tests share
# one definition instead of re-listing {"restricted", "phi"} per call site.
RESTRICTED_LEVELS: frozenset[SensitivityLevel] = frozenset(
    {SensitivityLevel.RESTRICTED, SensitivityLevel.PHI}
)


def parse_level(raw: str) -> SensitivityLevel:
    """Parse a schema-declared sensitivity string into a level.

    Raises:
        InvariantViolation: the string is not a known level. Failing
            closed keeps a misconfigured schema from silently
            downgrading a field to a weaker level.
    """
    try:
        return SensitivityLevel(raw)
    except ValueError as exc:
        raise InvariantViolation(f"unknown sensitivity level {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class FieldSensitivity:
    """Declared sensitivity of one field, by key (never by value).

    ``key`` is the stable machine key from the published schema (P05
    §2); ``level`` is the declared label. The value object carries no
    field value — classification is a property of the declaration.
    """

    key: str
    level: SensitivityLevel


@dataclass(frozen=True, slots=True)
class SensitiveReference:
    """Marker replacing a sensitive value, referencing it by id.

    ``field_key`` identifies *which* field was redacted; ``reason`` is
    the stable wire reason (``"sensitive"``). The original content is
    never stored on the marker, so serializing the marker cannot leak
    the value through any path (``repr``, JSON, logs).
    """

    field_key: str
    reason: str = "sensitive"

    def to_marker(self) -> dict[str, Any]:
        """Render the wire marker (key identity, no content)."""
        return {"redacted": True, "reason": self.reason, "ref": self.field_key}


@dataclass(frozen=True, slots=True)
class SensitivityClassification:
    """Key -> level map for one case type version plus case default.

    Built once per read via :func:`classify` from the published schema;
    threaded into the redact helpers so no helper re-resolves the
    schema. ``case_level`` is the case's effective sensitivity
    (inherited from the type's ``sensitivity_default``); when the case
    itself is restricted/phi, the whole payload is treated as sensitive
    regardless of per-field labels (P01-US4 coarse flag).
    """

    fields: tuple[FieldSensitivity, ...] = ()
    case_level: SensitivityLevel = SensitivityLevel.INTERNAL

    def level_for(self, key: str) -> SensitivityLevel:
        """Return the declared level for ``key`` (``internal`` if undeclared).

        Why default to ``internal``: unknown keys are legacy data written
        before the schema declared the field. The schema is the contract
        (not the data), so undeclared keys must not trigger redaction.
        """
        for entry in self.fields:
            if entry.key == key:
                return entry.level
        return SensitivityLevel.INTERNAL

    def is_sensitive(self, key: str) -> bool:
        """Return True when ``key`` needs the sensitive-read grant."""
        if self.case_level.is_restricted():
            return True
        return self.level_for(key).is_restricted()

    def sensitive_keys(self) -> frozenset[str]:
        """Return every key whose value must be redacted without a grant."""
        if self.case_level.is_restricted():
            return frozenset(entry.key for entry in self.fields)
        return frozenset(entry.key for entry in self.fields if entry.level.is_restricted())


def classify(
    schema: CaseTypeSchema,
    *,
    case_sensitivity: str = "internal",
) -> SensitivityClassification:
    """Build the classification for a published schema + case default.

    Args:
        schema: the published ``CaseTypeVersion`` schema that declared
            each field's sensitivity. Only declared levels are read;
            values are never inspected.
        case_sensitivity: the case's effective sensitivity (inherited
            from the type's ``sensitivity_default`` at creation).

    Raises:
        InvariantViolation: an unknown sensitivity string appears in
            the schema or the case default (fail-closed).
    """
    fields = tuple(
        FieldSensitivity(key=field.key, level=parse_level(field.sensitivity))
        for field in schema.fields
    )
    return SensitivityClassification(fields=fields, case_level=parse_level(case_sensitivity))


def classify_keys(
    keys: Mapping[str, str],
    *,
    case_sensitivity: str = "internal",
) -> SensitivityClassification:
    """Build a classification directly from a key -> level map.

    Used by search / event paths that pre-resolve the union of
    sensitive keys across a tenant's published versions and hand the
    sealed set down without re-opening the schema store.
    """
    fields = tuple(
        FieldSensitivity(key=key, level=parse_level(level)) for key, level in keys.items()
    )
    return SensitivityClassification(fields=fields, case_level=parse_level(case_sensitivity))


def redact_case_fields(
    data: Mapping[str, Any],
    classification: SensitivityClassification,
    *,
    can_see_sensitive: bool,
) -> dict[str, Any]:
    """Redact case ``data`` fields the actor may not see (P11-US6).

    Keys are preserved; restricted values are replaced with a
    :class:`SensitiveReference` marker carrying the field key as
    ``ref``. Returns a fresh dict; the input is never mutated so the
    caller cannot write the redacted view back into the aggregate.
    """
    if can_see_sensitive:
        return dict(data)
    if classification.case_level.is_restricted():
        # why: coarse P01-US4 path — the whole case is sensitive, so
        # every present key is masked by reference, not just the ones
        # whose field label is restricted.
        return {key: SensitiveReference(field_key=key).to_marker() for key in data}
    sensitive = classification.sensitive_keys()
    if not sensitive:
        return dict(data)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in sensitive:
            out[key] = SensitiveReference(field_key=key).to_marker()
        else:
            out[key] = value
    return out


def redact_by_keys(
    payload: Mapping[str, Any],
    sensitive_keys: frozenset[str],
    *,
    reason: str = "sensitive",
) -> dict[str, Any]:
    """Recursively redact ``payload`` entries whose key is sensitive.

    Walks nested mappings and lists; redaction is by key identity only
    — values are never scanned. Each redacted entry becomes a
    :class:`SensitiveReference` marker so the consumer learns *which*
    key was masked (the ``ref``) without learning the content.
    """
    lowered = frozenset(key.lower() for key in sensitive_keys)
    return {
        str(key): _redact_entry(str(key), value, lowered, reason) for key, value in payload.items()
    }


def _redact_entry(
    key: str,
    value: Any,
    sensitive_keys: frozenset[str],
    reason: str,
) -> Any:
    if key.lower() in sensitive_keys:
        return SensitiveReference(field_key=key, reason=reason).to_marker()
    return _redact_value(value, sensitive_keys, reason)


def _redact_value(value: Any, sensitive_keys: frozenset[str], reason: str) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for nested_key, nested_value in value.items():
            key_str = str(nested_key)
            redacted[key_str] = _redact_entry(key_str, nested_value, sensitive_keys, reason)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, sensitive_keys, reason) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, sensitive_keys, reason) for item in value)
    return value


def redact_log_payload(
    payload: Mapping[str, Any],
    classification: SensitivityClassification,
) -> dict[str, Any]:
    """Redact a log payload's sensitive entries by reference id.

    The returned dict carries ``ref`` markers instead of values, so the
    log line records *which* fields were present without recording
    their content. Non-sensitive keys pass through untouched.
    """
    return redact_by_keys(payload, classification.sensitive_keys())


def redact_event_payload(
    payload: Mapping[str, Any],
    classification: SensitivityClassification,
) -> dict[str, Any]:
    """Redact an audit/event payload's sensitive entries by reference id.

    Same key-based contract as :func:`redact_log_payload` with a
    distinct name so audit call sites state their intent (P09 audit
    payloads reference protected values by id only, Constitution V).
    """
    return redact_by_keys(payload, classification.sensitive_keys())


def redact_search_snippet(
    snippet: Mapping[str, Any],
    classification: SensitivityClassification,
    *,
    can_see_sensitive: bool,
) -> dict[str, Any]:
    """Redact a search result snippet's sensitive excerpts (P05-US3 E3).

    ``snippet`` maps field key -> excerpt text. Without the
    sensitive-read grant, excerpts for restricted keys are replaced
    with a ``ref`` marker naming the field; the underlying case data is
    unaffected and the excerpt text never leaves the process.
    """
    if can_see_sensitive:
        return dict(snippet)
    sensitive = classification.sensitive_keys()
    if not sensitive:
        return dict(snippet)
    out: dict[str, Any] = {}
    for key, value in snippet.items():
        if key in sensitive:
            out[key] = SensitiveReference(field_key=key).to_marker()
        else:
            out[key] = value
    return out


def redacted_refs(
    data: Mapping[str, Any],
    classification: SensitivityClassification,
) -> tuple[str, ...]:
    """Return the sorted field keys that WOULD be redacted.

    Observability helper: callers log the ref list / count for
    forensics without ever logging the values themselves.
    """
    sensitive = classification.sensitive_keys()
    if classification.case_level.is_restricted():
        return tuple(sorted(str(key) for key in data))
    return tuple(sorted(str(key) for key in data if str(key) in sensitive))


__all__ = [
    "RESTRICTED_LEVELS",
    "FieldSensitivity",
    "SensitivityClassification",
    "SensitivityLevel",
    "SensitiveReference",
    "classify",
    "classify_keys",
    "parse_level",
    "redact_by_keys",
    "redact_case_fields",
    "redact_event_payload",
    "redact_log_payload",
    "redact_search_snippet",
    "redacted_refs",
]
