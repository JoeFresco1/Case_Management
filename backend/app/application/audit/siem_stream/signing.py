"""v1 HMAC-SHA-256 signing + offline verification aids (P09-US9 / FM-046 / ADR-009).

# INTEGRATION_SEAM: Azure SIEM — **Gated**

Pure functions: canonical batch payload encoding, per-tenant key
derivation, signature production, and constant-time verification. The
signature is ``SHA-256(secret + canonical(batch_payload))`` in v1 —
HMAC-SHA-256 under a tenant-scoped key. Production-grade asymmetric
signing (RSA / ECDSA) and Key Vault-resolved keys land with the Azure
adapter (ADR-009); until then the deterministic synthetic key keeps
tests reproducible. :func:`recompute_row_hash` re-exports the domain
chain hash so a SIEM consumer verifies rows with the platform's own
rule. Split out of the former single ``siem_stream`` module by the R2
structural refactor; everything here is re-exported from
``app.application.audit.siem_stream`` unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

from app.domain.audit import compute_hash


def canonical_batch_payload(payload: dict[str, Any]) -> bytes:
    """Stable JSON encoding for the signed batch payload.

    Mirrors :func:`app.domain.audit.canonical_json` but returns bytes
    so the HMAC implementation does not have to re-encode. ``sort_keys``
    and a single-space separator are the same rules the audit chain
    uses (Constitution XIII.5) so an auditor can verify both surfaces
    with the same toolchain.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def derive_tenant_key(tenant_id: UUID, *, synthetic_secret: str) -> bytes:
    """Derive the per-tenant signing key for the v1 HMAC implementation.

    Why a derivation: the secret store holds one synthetic-environment
    secret; the per-tenant key is a SHA-256 over the tenant id so a
    leak of one tenant's key does not weaken another's. The
    production Azure adapter replaces this with a Key Vault-resolved
    per-tenant key (ADR-009 Gated).
    """

    h = hashlib.sha256()
    h.update(synthetic_secret.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(tenant_id).encode("utf-8"))
    return h.digest()


def sign_payload(
    *,
    tenant_id: UUID,
    payload: dict[str, Any],
    key_id: str,  # noqa: ARG001
    synthetic_secret: str = "synthetic-environment-secret",  # noqa: S107 - synthetic env marker only
) -> str:
    """Produce the v1 HMAC-SHA-256 signature for ``payload``.

    The signature is the hex digest of the HMAC over
    ``canonical_batch_payload(payload)`` keyed with
    :func:`derive_tenant_key`. ``key_id`` is a label that ties the
    signature to a key version; the v1 implementation uses a single
    synthetic key, the Azure adapter uses a Key Vault reference.
    """

    key = derive_tenant_key(tenant_id, synthetic_secret=synthetic_secret)
    mac = hmac.new(key, canonical_batch_payload(payload), hashlib.sha256)
    return mac.hexdigest()


def verify_signature(
    *,
    tenant_id: UUID,
    payload: dict[str, Any],
    key_id: str,
    signature: str,
    synthetic_secret: str = "synthetic-environment-secret",  # noqa: S107 - synthetic env marker only
) -> bool:
    """Recompute the signature and constant-time-compare it.

    Why a separate function: the auditor's tooling (and the acceptance
    test) must be able to verify a batch without going through the
    production ``sign_payload`` path. ``hmac.compare_digest`` is the
    constant-time comparison so a timing side-channel cannot reveal a
    prefix of the expected signature.
    """

    expected = sign_payload(
        tenant_id=tenant_id,
        payload=payload,
        key_id=key_id,
        synthetic_secret=synthetic_secret,
    )
    return hmac.compare_digest(expected, signature)


def recompute_row_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """Expose :func:`app.domain.audit.compute_hash` to the SIEM consumer.

    Why a re-export here: the SIEM verification recipe is "for each
    row, recompute ``SHA-256(prev_hash + canonical(payload))``". The
    auditor can re-implement the rule, but exposing the same helper
    the platform uses keeps the two implementations in lockstep.
    """

    return compute_hash(prev_hash, payload)


__all__ = [
    "canonical_batch_payload",
    "compute_hash",
    "derive_tenant_key",
    "hashlib",
    "hmac",
    "json",
    "recompute_row_hash",
    "sign_payload",
    "verify_signature",
]
