"""Delivery signing helpers (pure): HMAC signature value + JWT bearer."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import ValidationFailed
from app.domain.outbound_webhook import SigningAlgorithm

from .constants import JWT_BEARER_TTL_SECONDS


def sign_delivery(
    *,
    body: bytes,
    timestamp: str,
    secret: str,
    algorithm: SigningAlgorithm,
) -> str:
    """Compute the signature header value for an outbound delivery.

    The signed string is ``f"{timestamp}.{body}"`` — the same
    canonicalization the inbound surface (ADR-007) uses, so a
    subscriber that already implements the verify path for inbound
    webhooks can reuse the same code for outbound.

    Returned format matches the inbound surface:
    ``sha256=<hex>`` for HMAC. The ``JWT_BEARER`` algorithm
    produces a different header value (the JWT itself); the
    ``X-Webhook-Algorithm`` header tells the subscriber which
    scheme is in use.
    """
    if algorithm is SigningAlgorithm.HMAC_SHA256:
        mac = hmac.new(
            secret.encode("utf-8"),
            msg=f"{timestamp}.".encode() + body,
            digestmod=hashlib.sha256,
        )
        return f"sha256={mac.hexdigest()}"
    # JWT_BEARER is asymmetric: the HMAC is still computed and
    # sent in the signature header (so a subscriber that only
    # supports HMAC can still verify the body), AND the JWT goes
    # in ``Authorization``. The function returns the HMAC; the
    # bearer is built separately in :func:`build_jwt_bearer`.
    if algorithm is SigningAlgorithm.JWT_BEARER:
        mac = hmac.new(
            secret.encode("utf-8"),
            msg=f"{timestamp}.".encode() + body,
            digestmod=hashlib.sha256,
        )
        return f"sha256={mac.hexdigest()}"
    raise ValidationFailed(f"unsupported signing_algorithm: {algorithm!r}")


def build_jwt_bearer(
    *,
    secret: str,
    subscription_id: UUID,
    delivery_id: UUID,
    event_id: UUID,
    now: datetime,
    ttl_seconds: int = JWT_BEARER_TTL_SECONDS,
) -> str:
    """Build a short-lived HS256 JWT bearer for outbound delivery.

    The token is a minimal RFC 7519 structure: ``{"alg":"HS256",
    "typ":"JWT"}`` header, claims ``sub``, ``sub_id``, ``dlv_id``,
    ``evt_id``, ``iat``, ``exp``, and ``jti``. The signature is
    HMAC-SHA256 over the base64url(header).base64url(payload) string.

    Why a hand-rolled JWT rather than a library: the dependency
    list in ``pyproject.toml`` does not include ``pyjwt``; adding
    one just for the outbound surface would expand the surface
    area. A 30-line HS256 builder is small enough to keep here,
    with the structural assertions (three segments separated by
    ``.``) under the test suite.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    iat = int(now.timestamp())
    exp = iat + ttl_seconds
    claims = {
        "sub": "outbound-webhook",
        "sub_id": str(subscription_id),
        "dlv_id": str(delivery_id),
        "evt_id": str(event_id),
        "iat": iat,
        "exp": exp,
        "jti": str(uuid4()),
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    claims_b64 = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    mac = hmac.new(secret.encode("utf-8"), signing_input, digestmod=hashlib.sha256)
    sig_b64 = _b64url(mac.digest())
    return f"{header_b64}.{claims_b64}.{sig_b64}"


def _b64url(data: bytes) -> str:
    """Base64url-encode ``data`` without padding (RFC 7515 §2).

    JWT spec requires unpadded base64url; ``base64.urlsafe_b64encode``
    returns padded output which must be stripped. We use this
    helper rather than the standard library directly so the
    test suite can assert on the exact wire format.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


__all__ = [
    "_b64url",
    "base64",
    "build_jwt_bearer",
    "datetime",
    "hashlib",
    "hmac",
    "json",
    "sign_delivery",
]
