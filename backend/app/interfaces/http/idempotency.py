"""Authenticate and reserve a mutation before atomically storing its HTTP response.

INTEGRATION_SEAM: idempotency persistence — the provider owns transaction
semantics; this HTTP adapter only hashes requests and serializes responses.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.application.idempotency.http_dependencies import IdempotencyHttpDependencyProvider
from app.config import get_settings
from app.deps import get_current_user
from app.domain.context import UserContext
from app.domain.errors import DependencyUnavailable

HEADER = "idempotency-key"


@dataclass(frozen=True, slots=True)
class IdempotencyContext:
    """Identify one authenticated tenant's mutation."""

    key: str
    tenant_id: UUID


def hash_request_body(body: bytes) -> str:
    """Return a content digest without persisting raw request data."""
    return hashlib.sha256(body).hexdigest()


def canonical_request_hash(body: bytes, *, method: str = "POST", path: str = "") -> str:
    """Hash operation identity and canonical JSON, or raw bytes for other media."""
    try:
        body_value: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body_value = {"raw_sha256": hash_request_body(body)}
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "body": body_value},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    """Return the safe common error shape even outside route exception handlers."""
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "details": None,
            "correlation_id": _correlation_id(request),
        },
    )


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Reserve and finalize a mutation in the same transaction as its effects."""

    def __init__(self, app: ASGIApp, *, scope_paths: tuple[str, ...] | None = None) -> None:
        """Configure protected path prefixes; an empty tuple explicitly disables handling."""
        super().__init__(app)
        self._scope_paths = ("/api/v1",) if scope_paths is None else scope_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Run read requests normally and protect keyed mutations before dispatch."""
        if request.method in ("GET", "HEAD", "OPTIONS") or not any(
            request.url.path.startswith(path) for path in self._scope_paths
        ):
            return await call_next(request)
        key = request.headers.get(HEADER, "").strip()
        if not key:
            if request.method == "POST":
                return _error(
                    request, 400, "validation_failed", "Idempotency-Key header is required"
                )
            return await call_next(request)
        if len(key) > 128:
            return _error(request, 400, "validation_failed", "Idempotency-Key is too long")
        return await self._handle_authenticated(request, call_next, key)

    async def _handle_authenticated(
        self, request: Request, call_next: RequestResponseEndpoint, key: str
    ) -> Response:
        """Authenticate before reserving the mutation; map auth failures safely."""
        try:
            user = await _authenticate_request(request)
            request.state.user = user
            return await self._mutate(request, call_next, key, user)
        except HTTPException as exc:
            message = exc.detail if isinstance(exc.detail, str) else "request authentication failed"
            return _error(request, exc.status_code, "unauthorized", message)
        except DependencyUnavailable:
            return _error(request, 503, "dependency_unavailable", "request persistence unavailable")

    async def _mutate(
        self, request: Request, call_next: RequestResponseEndpoint, key: str, user: UserContext
    ) -> Response:
        """Keep the SQL reservation alive through response buffering and finalization."""
        body = await request.body()
        operation_hash = canonical_request_hash(
            body, method=request.method, path=str(request.url.path) + "?" + request.url.query
        )
        # Replay cannot disclose a prior actor's response or bypass a role revocation.
        request_hash = canonical_request_hash(
            json.dumps(
                {
                    "operation": operation_hash,
                    "actor": str(user.user_id),
                    "roles": sorted(user.roles),
                    "system": user.is_system,
                }
            ).encode()
        )
        provider = getattr(request.app.state, "idempotency_http_dependencies", None)
        if provider is None:
            raise DependencyUnavailable("idempotency provider is not configured")
        provider = cast(IdempotencyHttpDependencyProvider, provider)
        async with provider.transaction(
            tenant_id=user.tenant_id,
            key=key,
            request_hash=request_hash,
            correlation_id=_correlation_id(request),
        ) as transaction:
            if transaction.prior is not None:
                if transaction.prior["request_hash"] != request_hash:
                    return _error(
                        request, 409, "conflict", "Idempotency-Key belongs to another request"
                    )
                return _replay(transaction.prior)
            request.state.idempotency = IdempotencyContext(key=key, tenant_id=user.tenant_id)
            request.state.idempotency_request_hash = request_hash
            request.state.idempotency_session = transaction.session
            try:
                response = await call_next(request)
                # Consume the stream before committing: serialization failure must roll back.
                buffered = b"".join([chunk async for chunk in response.body_iterator])
                if 200 <= response.status_code < 300:
                    await transaction.finalize(
                        response_status=response.status_code,
                        response_body={
                            "__http_response_v1__": {
                                "body": base64.b64encode(buffered).decode("ascii"),
                                "headers": [
                                    [k.decode("latin-1"), v.decode("latin-1")]
                                    for k, v in response.raw_headers
                                    if k.lower() not in (b"x-correlation-id", b"idempotency-replay")
                                ],
                            }
                        },
                        correlation_id=_correlation_id(request),
                    )
                result = Response(content=buffered, status_code=response.status_code)
                result.raw_headers = response.raw_headers
                return result
            finally:
                request.state.idempotency_session = None


def _replay(prior: dict[str, object]) -> Response:
    """Restore stored bytes and headers; retain compatibility with older JSON responses."""
    body = prior["response_body"]
    if isinstance(body, dict) and "__http_response_v1__" in body:
        stored = body["__http_response_v1__"]
        response = Response(
            content=base64.b64decode(stored["body"]), status_code=int(str(prior["response_status"]))
        )
        response.raw_headers = [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in stored["headers"]
        ]
    else:
        response = JSONResponse(status_code=int(str(prior["response_status"])), content=body)
    response.headers["idempotency-replay"] = "true"
    return response


async def _authenticate_request(request: Request) -> UserContext:
    """Use the same authenticated identity dependency as the route, before replay lookup."""
    override = request.app.dependency_overrides.get(get_current_user)
    if override is not None:
        # Explicit application dependency overrides are trusted composition, never header input.
        resolver = cast(Callable[[], UserContext | Awaitable[UserContext]], override)
        resolved = resolver()
        user = await resolved if inspect.isawaitable(resolved) else resolved
    else:
        user = get_current_user(
            settings=get_settings(), x_dev_user=request.headers.get("x-dev-user")
        )
    tenant_header = request.headers.get("x-dev-tenant")
    if tenant_header and tenant_header.lower() != str(user.tenant_id):
        raise HTTPException(status_code=401, detail="tenant does not match authenticated identity")
    return user


def _correlation_id(request: Request) -> str:
    """Read the current request correlation, never a previous replay's correlation."""
    ctx = getattr(request.state, "correlation", None)
    if ctx is not None:
        return str(ctx.correlation_id)
    return request.headers.get("x-correlation-id", "")


__all__ = [
    "HEADER",
    "IdempotencyContext",
    "IdempotencyMiddleware",
    "canonical_request_hash",
    "hash_request_body",
]
