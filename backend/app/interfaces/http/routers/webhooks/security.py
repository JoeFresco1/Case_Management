"""Transport-level HMAC verification for inbound webhooks (P04-US8 / FM-030, ADR-007).

Authorization gate #1 of the webhooks router (the package docstring
describes both gates): HMAC verification using the channel's shared
secret. The signature is computed over the raw request body and the
``X-Webhook-Timestamp`` header; the secret is resolved through the
``SecretProvider`` port (``AGENTS.md §12``). A failed HMAC returns
``401`` so the platform never confirms the channel's existence to a
non-authenticated caller (Constitution V).

The router is the only place the secret is read — the use case never
sees it. In the synthetic posture the HMAC secret lives in the local
``SecretProvider`` (file or env); the P14 connector for outbound
webhooks (FM-056) is a separate seam. This module is the
**inbound** transport surface (FM-030).
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, status

from app.domain.ports import SecretProvider

# Standard HMAC header. The signature scheme is ``sha256=<hex>`` to
# match the convention used by GitHub / Stripe / etc. and to leave
# room for a future algorithm upgrade without breaking callers.
_HMAC_HEADER = "x-webhook-signature"
_WEBHOOK_ID_HEADER = "x-webhook-id"
_WEBHOOK_TIMESTAMP_HEADER = "x-webhook-timestamp"
_WEBHOOK_SOURCE_HEADER = "x-webhook-source"

# Replay window. We refuse signatures whose timestamp is more than
# 5 minutes off the local clock so a captured signature cannot be
# replayed indefinitely. 5 minutes is a conservative default; the
# channel's policy could override in a future slice.
_HMAC_REPLAY_WINDOW_SECONDS = 5 * 60

# Secret-name template. The HMAC secret for ``{channel_key}`` is
# resolved as ``webhook_secret_{channel_key}`` from the
# ``SecretProvider``. The naming convention is documented in ADR-007.
_SECRET_NAME_TEMPLATE = "webhook_secret_{channel_key}"  # noqa: S105 — secret-name template, not a value


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def _expected_secret_name(channel_key: str) -> str:
    """Return the secret-provider key for ``channel_key``'s HMAC secret."""
    return _SECRET_NAME_TEMPLATE.format(channel_key=channel_key)


def compute_signature(*, body: bytes, timestamp: str, secret: str) -> str:
    """Compute the canonical HMAC-SHA256 signature for a webhook request.

    The signed string is ``f"{timestamp}.{body}"`` per the GitHub /
    Stripe convention. The signature is returned as ``sha256=<hex>``.
    The router is the only place this happens — the use case does not
    see the secret.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        msg=f"{timestamp}.".encode() + body,
        digestmod=hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison.

    ``hmac.compare_digest`` is the right primitive; we wrap it so the
    router module does not need to know the import name.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _verify_timestamp(timestamp: str, *, now_epoch: int) -> None:
    """Refuse signatures whose timestamp is outside the replay window.

    Raises ``HTTPException`` 401 ``invalid_webhook_timestamp`` when the
    value is not a unix epoch integer, and 401
    ``webhook_timestamp_out_of_window`` when it is more than
    ``_HMAC_REPLAY_WINDOW_SECONDS`` away from ``now_epoch``.
    """
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_timestamp",
                "message": f"{_WEBHOOK_TIMESTAMP_HEADER} must be a unix epoch integer",
            },
        ) from exc
    if abs(now_epoch - ts) > _HMAC_REPLAY_WINDOW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "webhook_timestamp_out_of_window",
                "message": "webhook signature timestamp is outside the replay window",
            },
        )


async def _verify_hmac(
    *,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: SecretProvider,
    channel_key: str,
    now_epoch: int,
) -> str:
    """Verify the HMAC signature and return the source-system identifier.

    Returns the ``source_system`` on success; raises ``HTTPException``
    with ``401`` on any failure (missing headers, bad signature, bad
    timestamp, unknown channel secret).

    The function never reveals which check failed beyond a coarse
    error code so a probing caller cannot enumerate channels.
    """
    if not signature or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "missing_webhook_signature",
                "message": f"{_HMAC_HEADER} and {_WEBHOOK_TIMESTAMP_HEADER} are required",
            },
        )
    _verify_timestamp(timestamp, now_epoch=now_epoch)
    secret_name = _expected_secret_name(channel_key)
    try:
        secret_value = await secret.get(secret_name)
    except KeyError as exc:
        # Unknown channel secret => indistinguishable from a bad
        # signature at the API surface. The redaction list in the
        # telemetry layer must also mask the secret name.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_signature",
                "message": "webhook signature verification failed",
            },
        ) from exc
    expected = compute_signature(body=body, timestamp=timestamp, secret=secret_value)
    if not _constant_time_eq(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_signature",
                "message": "webhook signature verification failed",
            },
        )
    # The source system is the secret name minus the prefix. Two
    # different channels sharing a secret is a configuration error
    # and is detected at channel registration, not here.
    return channel_key
