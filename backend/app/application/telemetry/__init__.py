"""Application telemetry helpers (pure, no infrastructure imports).

Currently hosts the key-based sensitive-value redaction core shared by
log lines, audit payloads, and error responses. The application layer
depends only on ``app.domain``; this subpackage additionally depends on
nothing but the stdlib so both infrastructure adapters and HTTP
interfaces can import it without violating the layer contract.
"""

from __future__ import annotations

from app.application.telemetry.redaction import redact_sensitive

__all__: list[str] = ["redact_sensitive"]
