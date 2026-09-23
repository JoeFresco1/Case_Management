"""Constants and the shared logger for the outbound webhook surface.

The logger name is the literal ``app.application.webhooks.outbound``, not
``__name__``: before the R2 package split every record was emitted under the
single module name, and telemetry filters keyed on logger names must not
change behavior because of a structural refactor.
"""

from __future__ import annotations

import logging
from typing import Final

# why a literal name: preserves the pre-split record name (see module docstring).
logger = logging.getLogger("app.application.webhooks.outbound")

# Mirror of the outbox dispatcher cap (AGENTS.md §16.1 / outbox.py).
# After ``MAX_ATTEMPTS`` failed attempts, the row is dead-lettered.
MAX_ATTEMPTS: Final[int] = 5

# Per-attempt wall-clock timeout. A subscriber that hangs would
# otherwise pin the dispatch loop indefinitely; the row remains
# durable and will be retried on the next tick.
ATTEMPT_TIMEOUT_SECONDS: Final[float] = 10.0

# Replay window the platform suggests to subscribers. We bake the
# same window into our signature header's ``X-Webhook-Timestamp``
# so a subscriber that applies the window is consistent with the
# inbound surface (ADR-007). The platform does not enforce the
# window on the outbound side — the subscriber is the consumer of
# the signature.
SIGNATURE_TIMESTAMP_HEADER: Final[str] = "X-Webhook-Timestamp"
SIGNATURE_HEADER: Final[str] = "X-Webhook-Signature"
SIGNATURE_ID_HEADER: Final[str] = "X-Webhook-Id"
SIGNATURE_ALG_HEADER: Final[str] = "X-Webhook-Algorithm"
EVENT_ID_HEADER: Final[str] = "X-Webhook-Event-Id"
EVENT_TYPE_HEADER: Final[str] = "X-Webhook-Event-Type"
DELIVERY_ID_HEADER: Final[str] = "X-Webhook-Delivery-Id"
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-Id"
AUTHORIZATION_HEADER: Final[str] = "Authorization"

# JWT bearer header parameters. We sign with HS256 using the
# subscription secret. ``exp`` is set to ``now + 5 minutes`` so a
# captured bearer cannot be replayed indefinitely — the subscriber
# should treat the bearer as a one-shot delivery proof.
JWT_BEARER_TTL_SECONDS: Final[int] = 5 * 60

# Permanent-failure HTTP status codes. A 4xx other than 408/429 is
# a "this delivery will never succeed" signal; we move the row to
# DEAD_LETTERED on the first occurrence. 408 and 429 are retried.
_PERMANENT_FAILURE_STATUSES: Final[frozenset[int]] = frozenset(
    {
        400,
        401,
        402,
        403,
        404,
        405,
        406,
        410,
        411,
        412,
        413,
        414,
        415,
        416,
        417,
        418,
        421,
        422,
        423,
        424,
        426,
        428,
        431,
        451,
    }
)


__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "AUTHORIZATION_HEADER",
    "CORRELATION_ID_HEADER",
    "DELIVERY_ID_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "JWT_BEARER_TTL_SECONDS",
    "MAX_ATTEMPTS",
    "SIGNATURE_ALG_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_ID_HEADER",
    "SIGNATURE_TIMESTAMP_HEADER",
    "_PERMANENT_FAILURE_STATUSES",
    "Final",
    "logging",
    "logger",
]
