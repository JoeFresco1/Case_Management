"""Constants for the SIEM stream and signed audit-batch surface (P09-US8, US9 / FM-046).

# INTEGRATION_SEAM: Azure SIEM — **Gated**

Pagination caps (AGENTS.md §17.1), the signed-batch row cap, the v1
signature algorithm / key labels (ADR-009), and the reserved keys of a
signed-batch payload. Split out of the former single ``siem_stream``
module by the R2 structural refactor; every value is re-exported from
``app.application.audit.siem_stream`` unchanged.
"""

from __future__ import annotations

from typing import Final

# AGENTS.md §17.1 — pagination envelope.
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

# Maximum rows a single signed batch may cover. Larger windows force a
# longer recompute on the auditor side and inflate the signature
# payload; the SIEM consumer typically wants a smaller, more frequent
# batch. A signed batch over 5000 rows is a sign the auditor wants an
# export, not a signed attestation — the use case rejects the
# request.
MAX_BATCH_ROWS: Final[int] = 5000

# Signature algorithm advertised by the v1 batch implementation. The
# HMAC-SHA-256 choice is deliberate: a symmetric keyed-hash is enough
# to attest "the platform produced this batch with the secret it
# holds" and does not require key-distribution infrastructure on the
# auditor side beyond the ``key_id`` label. Asymmetric signing (RSA /
# ECDSA) is the production seam; the algorithm string in the batch
# payload is the explicit version pin.
ALGORITHM_HMAC_SHA256: Final[str] = "HMAC-SHA-256"
ALGORITHM_LABEL: Final[str] = "v1"

# Local-dev signing-key identifier. The Azure adapter uses a key
# resolved through ``SecretProvider``; the v1 implementation falls
# back to a deterministic per-tenant key derived from a fixed
# synthetic-environment secret so tests are reproducible.
DEFAULT_KEY_ID: Final[str] = "synthetic-v1"

# Reserved keys in a signed batch payload. Using named constants is
# the AGENTS.md §4.R4 readability requirement: a reader of the
# codebase can locate the contract from this module.
BATCH_KEY_ALGORITHM: Final[str] = "algorithm"
BATCH_KEY_KEY_ID: Final[str] = "key_id"
BATCH_KEY_SIGNATURE: Final[str] = "signature"
BATCH_KEY_PAYLOAD: Final[str] = "payload"
BATCH_KEY_FIRST_EVENT_ID: Final[str] = "first_event_id"
BATCH_KEY_LAST_EVENT_ID: Final[str] = "last_event_id"
BATCH_KEY_ROW_COUNT: Final[str] = "row_count"
BATCH_KEY_TENANT_ID: Final[str] = "tenant_id"
BATCH_KEY_CREATED_AT: Final[str] = "created_at"

__all__ = [
    "ALGORITHM_HMAC_SHA256",
    "ALGORITHM_LABEL",
    "BATCH_KEY_ALGORITHM",
    "BATCH_KEY_CREATED_AT",
    "BATCH_KEY_FIRST_EVENT_ID",
    "BATCH_KEY_KEY_ID",
    "BATCH_KEY_LAST_EVENT_ID",
    "BATCH_KEY_PAYLOAD",
    "BATCH_KEY_ROW_COUNT",
    "BATCH_KEY_SIGNATURE",
    "BATCH_KEY_TENANT_ID",
    "DEFAULT_KEY_ID",
    "DEFAULT_PAGE_LIMIT",
    "Final",
    "MAX_BATCH_ROWS",
    "MAX_PAGE_LIMIT",
]
