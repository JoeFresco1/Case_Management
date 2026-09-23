"""Local HTTP transport for outbound webhook deliveries (FM-056).

# INTEGRATION_SEAM: outbound transport

This module is the local adapter for the
:class:`OutboundDeliveryTransport` Protocol (in
``app.domain.ports``). The production seam is a hosted HTTP client
(``httpx.AsyncClient`` in Azure Container Apps; or Azure Event Grid
if the customer prefers a managed topic). The application layer
never imports ``httpx`` directly; the seam keeps the test suite
substitutable.

The transport is intentionally narrow: the application layer
constructs the request (signs the body, attaches headers) and the
transport's only responsibility is to **POST** and return the
outcome. There is no retry logic in the transport — the application
layer owns the retry policy.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.domain.ports import TransportResult

logger = logging.getLogger(__name__)


class HttpXOutboundTransport:
    """``httpx``-backed outbound delivery transport.

    Thread safety: a single ``httpx.AsyncClient`` is created per
    instance; the client is internally thread-safe and connection-
    pooled, so the application layer may share one transport across
    the whole process. The ``aclose`` method releases the
    connection pool at shutdown.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> TransportResult:
        """POST ``body`` to ``url`` with ``headers``; return the outcome.

        Returns a :class:`TransportResult` with ``success=True`` for
        HTTP 2xx; ``success=False`` for any other status, transport
        error, or timeout. The caller (the application layer) is
        responsible for retry / dead-letter policy.
        """
        client = self._client
        created = False
        if client is None:
            client = httpx.AsyncClient(timeout=timeout_seconds)
            created = True
        try:
            try:
                response = await client.post(url, content=body, headers=headers)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "outbound_webhook.timeout",
                    extra={"url": url, "error": repr(exc)},
                )
                return TransportResult(success=False, status_code=None, error=repr(exc))
            except httpx.HTTPError as exc:
                # All other httpx errors (connect, read, network).
                logger.warning(
                    "outbound_webhook.transport_error",
                    extra={"url": url, "error": repr(exc)},
                )
                return TransportResult(success=False, status_code=None, error=repr(exc))
            success = 200 <= response.status_code < 300
            if not success:
                # Truncate the body so a 5xx page that includes an
                # HTML stack trace does not blow up the row.
                error = (
                    f"http {response.status_code}: {response.text[:200] if response.text else ''}"
                )
                return TransportResult(
                    success=False,
                    status_code=response.status_code,
                    error=error,
                )
            return TransportResult(success=True, status_code=response.status_code)
        finally:
            if created and client is not None:
                await client.aclose()

    async def aclose(self) -> None:
        """Close the underlying client (only if we own it)."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class RecordingOutboundTransport:
    """Test double that records every call without making HTTP requests.

    The application layer's tests use this to assert on the request
    shape (URL, body, headers) without a real HTTP server. The
    transport returns whatever the test pre-loaded via ``set_response``
    or ``set_exception``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[TransportResult] = []
        self._exceptions: list[Exception] = []

    def set_response(self, result: TransportResult) -> None:
        self._responses.append(result)

    def set_exception(self, exc: Exception) -> None:
        self._exceptions.append(exc)

    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> TransportResult:
        self.calls.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._exceptions:
            exc = self._exceptions.pop(0)
            raise exc
        if self._responses:
            return self._responses.pop(0)
        return TransportResult(success=True, status_code=200)

    async def aclose(self) -> None:
        return None


__all__ = [
    "HttpXOutboundTransport",
    "RecordingOutboundTransport",
]
