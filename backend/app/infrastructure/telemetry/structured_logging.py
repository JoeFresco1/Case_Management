"""Structured logging (structlog JSON) with redaction.

AGENTS.md §12 telemetry seam + R12 (secrets/PHI redaction) + §18
(observability). Every log line emitted through ``structlog.get_logger()``
after :func:`configure_structlog` has run includes the binding for
``correlation_id``, ``tenant_id``, and ``actor_id`` when they have been set
via :func:`app.infrastructure.telemetry.correlation.bind_*` (or any code path
that called ``structlog.contextvars.bind_contextvars`` with those keys).

The processor order matters and is intentionally minimal:

1. ``merge_contextvars`` — pulls any bound keys (correlation_id, tenant_id,
   actor_id, method, path, ...) into the event dict.
2. ``add_log_level`` — adds ``level`` (``"info"`` etc.).
3. ``TimeStamper(fmt="iso")`` — UTC ISO-8601 timestamp.
4. ``_redact`` — recursively walks the event dict and replaces any value
   whose key matches the redaction list with ``"[REDACTED]"``.
5. ``JSONRenderer`` — emits the final JSON line.

Redaction list (AGENTS.md §18, Constitution V):
    * ``ssn``
    * ``phi``
    * ``secret``
    * ``password``
    * ``authorization`` (HTTP header name)
    * ``x-dev-user`` / ``x-dev-tenant`` (header names — never log their
      values, but we also redact header dicts by exact key match)
    * ``token`` / ``refresh_token`` / ``session_id`` / ``cookie`` /
      ``bearer`` / ``private_key`` / ``client_secret`` (ADR-019)
    * ``dob`` / ``birth`` / ``address`` / ``phone`` / ``email`` /
      ``passport`` / ``license`` / ``tax_id`` / ``medical`` / ``health`` /
      ``diagnosis`` (FM-011 PHI needles — mirrors
      ``app.domain.events._PHI_KEY_NEEDLES`` plus classification keys
      such as ``medical_record`` via the ``medical`` substring)

The match is case-insensitive and substring-aware: a key named
``user_ssn`` or ``userSecret`` matches. Values are NEVER compared; we
redact by key only to keep the hot path allocation-free.

# INTEGRATION_SEAM: encryption-at-rest — Azure KeyVault field encryption

The structured-logging redaction list above protects log lines and
error responses, but it does NOT encrypt field values at rest. The
first slice stores ``cases.data_json`` in plain text inside the
``cases`` row; an attacker with database read access would see the
cleartext of every field, including ones whose schema sensitivity is
``restricted`` or ``phi``.

The seam is annotated here so the encryption-at-rest work has a single
documented home. When Azure KeyVault is wired into the persistence
layer (FM-029 / P11-US7), the encrypted-blob adapter will:

- wrap ``Case.data_json`` at write time with a per-tenant data key
  resolved through :class:`app.application.ports.SecretProvider`
  (``AzureKeyVault`` adapter, see AGENTS.md §12).
- store the ciphertext in ``cases.data_json`` (or a sidecar
  ``cases_data_ciphertext`` column added via Alembic).
- decrypt on read inside the SQL repository *before* the value reaches
  the application layer so the existing redactor continues to receive
  cleartext and can apply the field-level redaction above.

Until that adapter lands, callers must treat ``cases.data_json`` as
plaintext at rest and rely on the key-based redaction list and
field-level sensitivity to prevent leakage to logs / responses /
events. The local sqlite test profile and local Postgres are
acceptable for development only; production will not ship without
the KeyVault adapter in place.

Local adapter (no-op for v1): the field-level redaction done by
:mod:`app.application.cases.redactor` already prevents sensitive
values from reaching log payloads, error responses, or outbox events.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from app import context as request_context
from app.application.telemetry.redaction import (
    _REDACT_KEYS,
    _redact_value,
    redact_sensitive,
)

# Re-exported so existing import sites (tests, error model callers that
# predate the application-layer home) keep resolving: the canonical
# definitions live in ``app.application.telemetry.redaction``; this
# module re-exports them for backward compatibility.
__all__ = ["_REDACT_KEYS", "_redact_value", "configure_structlog", "redact_sensitive"]

# Canonical redaction core lives in ``app.application.telemetry.redaction``
# (pure, stdlib-only) so the HTTP error model can import it without
# violating the interfaces ↛ infrastructure layer contract. This module
# re-exports the names for backward compatibility with existing import
# sites (tests assert ``_REDACT_KEYS`` from here).


def _redact_processor(logger: Any, method_name: str, event_dict: Any) -> Any:  # noqa: ARG001
    """Structlog processor that redacts sensitive keys before rendering.

    The processor receives the merged event dict (after contextvars) and
    returns a new value with sensitive keys masked. Runs *after* merge so
    bound keys (which are added to the same dict) are also subject to
    redaction if their name is sensitive.
    """
    return _redact_value(event_dict)
    # why: mypy strict rejects ``Any`` returns unless we narrow here; the
    # processor chain is variadic so we accept the round-trip.


def _merge_request_context(logger: Any, method_name: str, event_dict: Any) -> Any:  # noqa: ARG001
    """Merge shared request context into a structlog event.

    The shared context is kept outside infrastructure so FastAPI dependency
    modules can bind identity data without importing this telemetry adapter.
    Explicit event fields retain structlog's normal precedence.
    """
    for key, value in request_context.current_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def configure_structlog(*, level: str) -> None:
    """Configure structlog + stdlib logging for the whole process.

    Safe to call multiple times — structlog replaces its config in place.
    Side effects: redirects ``logging.basicConfig`` so third-party libs
    (``uvicorn``, ``sqlalchemy``, ``alembic``) emit through the same
    structlog JSON pipeline.

    Note: ``cache_logger_on_first_use=False`` so tests can swap the
    ``logger_factory`` between cases (e.g. via ``structlog.configure``)
    without freezing the cache to the first factory they used.
    """
    level_value = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=level_value, format="%(message)s")
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _merge_request_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_value),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


__all__ = ["_REDACT_KEYS", "_redact_value", "configure_structlog", "redact_sensitive"]
