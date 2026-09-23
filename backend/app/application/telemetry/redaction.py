"""Key-based sensitive-value redaction (pure, no I/O).

This module owns the redaction core shared by log lines, audit payloads,
and error responses (AGENTS.md §18, Constitution V, FM-011): a static
list of sensitive key substrings plus a recursive redactor that replaces
matching values with ``"[REDACTED]"``.

Why this lives in ``app.application`` and not in infrastructure: the
HTTP error model (``app.interfaces.http.errors``) must redact 4xx bodies
before they reach the client, and the layer contract forbids interfaces
from importing infrastructure. The functions here are pure (stdlib
only — no structlog, no FastAPI), so both the telemetry adapter
(``app.infrastructure.telemetry.structured_logging``) and the error
model import the same single source of truth from here (AGENTS.md R10:
cross-cutting concerns live above domain, wired at the boundary).

Matching stays key-substring, case-insensitive; values are NEVER
inspected, so the hot path stays allocation-free and no secret/PHI
value can influence the redaction decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Redaction list — see module docstring. Lowercase substrings.
# why: both dash and underscore variants of header names appear in the
# pipeline (FastAPI receives ``X-Dev-User`` but other code may emit
# ``x_dev_user``); substring matching against both forms keeps the
# redaction list robust without needing an exhaustive allowlist.
#
# Extended by ADR-019 to cover keys produced by identity adapters
# (refresh_token, session_id, cookie, bearer) and the KeyVault seam
# (private_key, client_secret). Every key here MUST also be asserted by
# ``backend/tests/security/test_phie_redaction.py``.
#
# Extended for FM-011 (P11-US6 / P05-US3): the domain PHI guard
# (``app.domain.events._PHI_KEY_NEEDLES``) and the sensitivity
# classification (``app.domain.sensitivity``) resolve schema-declared
# keys such as ``dob``, ``medical_record``, ``phone``, ``address``.
# Log/event/error surfaces must redact those keys even when the
# payload nests them outside a case ``data`` blob, so the static
# telemetry list mirrors the PHI needles here. Matching stays
# key-substring, case-insensitive; values are never inspected.
_REDACT_KEYS: tuple[str, ...] = (
    "ssn",
    "phi",
    "secret",
    "password",
    "authorization",
    "x-dev-user",
    "x_dev_user",
    "x-dev-tenant",
    "x_dev_tenant",
    "x-api-key",
    "x_api_key",
    "api_key",
    "apikey",
    "token",
    "private_key",
    "client_secret",
    "refresh_token",
    "session_id",
    "cookie",
    "bearer",
    "dob",
    "birth",
    "address",
    "phone",
    "email",
    "passport",
    "license",
    "tax_id",
    "tax-id",
    "taxid",
    "medical",
    "health",
    "diagnosis",
)


def _is_sensitive_key(key: str) -> bool:
    """Return True when ``key`` contains any redaction-list substring.

    Args:
        key: The dict key to test (compared lower-cased).

    Returns:
        True when the key must be redacted; False otherwise.

    Failure mode:
        Never raises for string input; an empty key returns False.
    """
    lowered = key.lower()
    return any(token in lowered for token in _REDACT_KEYS)


def _redact_value(value: Any) -> Any:
    """Recursively replace values whose key matches the redaction list.

    Walks dicts and lists; leaves other types untouched (so numbers,
    strings, and exceptions are not mangled). The return value mirrors
    the structure of the input.

    Args:
        value: Any JSON-like structure to redact.

    Returns:
        The redacted structure with sensitive values replaced by
        ``"[REDACTED]"``.
    """
    if isinstance(value, Mapping):
        return {
            str(k): ("[REDACTED]" if _is_sensitive_key(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [_redact_value(item) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_sensitive(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of ``payload`` for non-logging callers.

    Public helper for audit payloads and error responses: error
    responses must never leak secrets/PHI to clients, and audit
    payloads must reference protected values by id only
    (Constitution V). Centralizing the redaction logic here means
    every surface that touches user data uses the same list.

    Args:
        payload: The mapping to redact (never mutated).

    Returns:
        A redacted copy of ``payload``.

    Failure mode:
        Never raises for mapping input; non-dict results are coerced
        to ``{"value": ...}`` so the public contract holds.
    """
    redacted = _redact_value(payload)
    # why: ``_redact_value`` may return non-dict types when the input is
    # not a Mapping (we never call it that way here, but mypy strict
    # cannot see that). Coerce to dict so the public contract holds.
    if not isinstance(redacted, dict):
        return {"value": redacted}
    return redacted


__all__ = ["_REDACT_KEYS", "_redact_value", "redact_sensitive"]
