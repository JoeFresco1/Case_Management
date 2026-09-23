"""Tests for the structured logging / correlation / redaction foundation.

AGENTS.md §18 observability contract: every log line MUST include
correlation_id; redaction MUST mask ssn/phi/secret/password; and the
SecretProvider port MUST resolve from env or fail-closed.

These tests do not depend on Docker or a live Postgres — they exercise
the telemetry layer in isolation (R1 WSL fallback).
"""

from __future__ import annotations

import io
import json
import logging
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
import structlog
from fastapi import FastAPI, Request

from app.config import Settings
from app.deps import get_current_user, get_secret_provider, register_local_user
from app.domain.context import UserContext
from app.infrastructure.telemetry.correlation import (
    ACTOR_ID,
    CORRELATION_ID,
    PATH,
    TENANT_ID,
    bind_actor,
    bind_correlation,
    bind_request_metadata,
    clear,
    current_context,
)
from app.infrastructure.telemetry.structured_logging import (
    configure_structlog,
    redact_sensitive,
)
from app.interfaces.http.errors import error_response

with warnings.catch_warnings():
    # Importing the composition root registers legacy startup hooks that
    # another owner is replacing; this test only needs its middleware.
    warnings.simplefilter("ignore", DeprecationWarning)
    from app.main import CorrelationMiddleware  # noqa: E402


@pytest.fixture
def captured_logs() -> io.StringIO:
    """Capture structlog JSON output for assertions."""
    buffer = io.StringIO()
    # why: reconfigure structlog to emit through the buffer (rather than
    # stdout) AND re-apply the production processor chain including the
    # redaction processor — calling ``configure_structlog`` first installs
    # the real pipeline; the subsequent ``structlog.configure`` call
    # swaps only the ``logger_factory`` without resetting processors.
    configure_structlog(level="INFO")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buffer))
    yield buffer
    # why: clear contextvars between tests so one test cannot leak state
    # into another (AGENTS.md §18 — contextvars are process-global).
    clear()


def _last_log_line(buffer: io.StringIO) -> dict[str, object]:
    text = buffer.getvalue().strip().splitlines()[-1]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Correlation binding
# ---------------------------------------------------------------------------


def test_bind_correlation__appears_on_every_log_line(captured_logs: io.StringIO) -> None:
    bind_correlation("corr-123")
    log = structlog.get_logger()
    log.info("test.event", payload="x")
    event = _last_log_line(captured_logs)
    assert event["correlation_id"] == "corr-123"


def test_bind_actor__attaches_tenant_and_actor(captured_logs: io.StringIO) -> None:
    bind_correlation("corr-xyz")
    bind_actor(actor_id="user-1", tenant_id="tenant-1")
    log = structlog.get_logger()
    log.info("test.actor_event")
    event = _last_log_line(captured_logs)
    assert event["correlation_id"] == "corr-xyz"
    assert event["actor_id"] == "user-1"
    assert event["tenant_id"] == "tenant-1"


def test_bind_request_metadata__captures_method_and_path(captured_logs: io.StringIO) -> None:
    bind_request_metadata(method="POST", path="/api/v1/cases")
    log = structlog.get_logger()
    log.info("test.request_event")
    event = _last_log_line(captured_logs)
    assert event["method"] == "POST"
    assert event[PATH] == "/api/v1/cases"


def test_clear__drops_correlation_id(captured_logs: io.StringIO) -> None:
    bind_correlation("corr-pre-clear")
    clear()
    log = structlog.get_logger()
    log.info("test.after_clear")
    event = _last_log_line(captured_logs)
    assert CORRELATION_ID not in event
    assert TENANT_ID not in event
    assert ACTOR_ID not in event


def _request(path: str, *, method: str = "GET", correlation_id: str = "") -> Request:
    """Build the minimal Starlette request scope needed by the middleware."""
    headers = []
    if correlation_id:
        headers.append((b"x-correlation-id", correlation_id.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
        }
    )


@pytest.mark.asyncio
async def test_correlation_middleware__handler_raises__clears_context() -> None:
    """A failed request cannot leak correlation or identity to the next request."""
    # given: stale context exists before the request.
    bind_correlation("stale-correlation")
    bind_actor(actor_id="stale-actor", tenant_id="stale-tenant")
    middleware = CorrelationMiddleware(FastAPI())

    async def raises(_: Request) -> object:
        """Raise a representative unhandled request failure."""
        raise RuntimeError("synthetic failure")

    # when: the handler raises after the middleware has installed request data.
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await middleware.dispatch(_request("/raises", correlation_id="request-correlation"), raises)

    # then: the middleware clears all ambient values even on failure.
    assert current_context() == {}


@pytest.mark.asyncio
async def test_missing_idempotency_error__uses_request_state_correlation() -> None:
    """Structured missing-key errors expose the request correlation id safely."""
    middleware = CorrelationMiddleware(FastAPI())

    async def create_without_key(request: Request) -> object:
        """Return the same safe error shape used by mutation boundaries."""
        return error_response(
            code="validation_failed",
            message="Idempotency-Key header is required",
            status_code=400,
            correlation_id=request.state.correlation.correlation_id,
        )

    # given / when: a mutation omits its idempotency key but supplies a CID.
    response = await middleware.dispatch(
        _request(
            "/api/v1/cases",
            method="POST",
            correlation_id="missing-key-correlation",
        ),
        create_without_key,
    )

    # then: both transport and structured error responses preserve that CID.
    assert response.status_code == 400
    assert response.headers["x-correlation-id"] == "missing-key-correlation"
    assert json.loads(response.body)["correlation_id"] == "missing-key-correlation"
    assert current_context() == {}


def test_current_context__snapshot_reflects_bound_keys() -> None:
    clear()
    bind_correlation("snap-1")
    bind_actor(actor_id="a", tenant_id="t")
    snapshot = current_context()
    assert snapshot[CORRELATION_ID] == "snap-1"
    assert snapshot[ACTOR_ID] == "a"
    assert snapshot[TENANT_ID] == "t"


def test_identity_dependency__binds_actor_without_importing_telemetry_adapter(
    captured_logs: io.StringIO,
) -> None:
    """Identity resolution still enriches logs through the shared context."""
    user_id = "00000000-0000-0000-0000-000000000001"
    tenant_id = "11111111-1111-1111-1111-111111111111"
    register_local_user(
        UserContext(
            user_id=UUID(user_id),
            tenant_id=UUID(tenant_id),
            display_name="Synthetic Intake",
            roles=("intake",),
        )
    )
    get_current_user(
        settings=Settings(app_env="local"),
        x_dev_user=user_id,
    )

    structlog.get_logger().info("test.identity_event")
    event = _last_log_line(captured_logs)
    assert event[ACTOR_ID] == user_id
    assert event[TENANT_ID] == tenant_id


# ---------------------------------------------------------------------------
# Redaction (AGENTS.md §18 + Constitution V)
# ---------------------------------------------------------------------------


def test_redact_sensitive__masks_ssn() -> None:
    payload = {"user_ssn": "111-22-3333", "name": "Alice"}
    out = redact_sensitive(payload)
    assert out["user_ssn"] == "[REDACTED]"
    assert out["name"] == "Alice"


def test_redact_sensitive__masks_password_and_token() -> None:
    payload = {
        "password": "hunter2",
        "authorization": "Bearer xyz",
        "x-api-key": "abc",
        "x_dev_user": "should-also-redact",
    }
    out = redact_sensitive(payload)
    assert out["password"] == "[REDACTED]"
    assert out["authorization"] == "[REDACTED]"
    assert out["x-api-key"] == "[REDACTED]"
    assert out["x_dev_user"] == "[REDACTED]"


def test_redact_sensitive__leaves_non_sensitive_keys_alone() -> None:
    payload = {"correlation_id": "abc", "tenant_id": "t", "actor_id": "u"}
    out = redact_sensitive(payload)
    assert out == payload


def test_redact_sensitive__walks_nested_structures() -> None:
    payload = {"data": {"secret_value": "shh", "ok": 1}, "items": [{"password": "x"}]}
    out = redact_sensitive(payload)
    assert out["data"]["secret_value"] == "[REDACTED]"
    assert out["data"]["ok"] == 1
    assert out["items"][0]["password"] == "[REDACTED]"


def test_logging_pipeline__redacts_ssn_in_real_event(captured_logs: io.StringIO) -> None:
    """End-to-end: a structlog event with an ssn key is redacted at render time."""
    bind_correlation("corr-redact")
    log = structlog.get_logger()
    log.info("user.record", user_ssn="111-22-3333", name="Bob")
    event = _last_log_line(captured_logs)
    assert event["user_ssn"] == "[REDACTED]"
    assert event["name"] == "Bob"
    assert event["correlation_id"] == "corr-redact"


def test_logging_pipeline__does_not_log_secret_values_via_event(
    captured_logs: io.StringIO,
) -> None:
    """A whole dict with sensitive keys is redacted on emission."""
    bind_correlation("corr-dict")
    log = structlog.get_logger()
    log.info("http.headers", headers={"authorization": "Bearer x", "x-dev-user": "u"})
    event = _last_log_line(captured_logs)
    headers = event["headers"]
    assert headers["authorization"] == "[REDACTED]"
    assert headers["x-dev-user"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# SecretProvider port (AGENTS.md §12)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    store = tmp_path / ".secrets.json"
    return Settings(secret_provider="local_env", secrets_local_store_path=store)


@pytest.mark.asyncio
async def test_secret_provider__resolves_from_env(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_SECRET_OUTBOX_TOKEN", "tok-123")
    from app.infrastructure.secrets.local_secret_provider import (  # noqa: PLC0415
        LocalSecretProvider,  # noqa: PLC0415
    )

    provider = LocalSecretProvider(store_path=tmp_settings.secrets_local_store_path)
    value = await provider.get("outbox_token")
    assert value == "tok-123"


@pytest.mark.asyncio
async def test_secret_provider__writes_and_reads_from_local_store(
    tmp_settings: Settings,
) -> None:
    from app.infrastructure.secrets.local_secret_provider import (  # noqa: PLC0415
        LocalSecretProvider,  # noqa: PLC0415
    )

    provider = LocalSecretProvider(store_path=tmp_settings.secrets_local_store_path)
    await provider.set_secret("smoke_token", "value-abc")
    resolved = await provider.get("smoke_token")
    assert resolved == "value-abc"
    # why: the store must persist so a process restart can resolve the
    # same secret (the whole point of using the local file as the
    # fallback backing store).
    assert tmp_settings.secrets_local_store_path.exists()


@pytest.mark.asyncio
async def test_secret_provider__raises_keyerror_when_missing(
    tmp_settings: Settings,
) -> None:
    from app.infrastructure.secrets.local_secret_provider import (  # noqa: PLC0415
        LocalSecretProvider,  # noqa: PLC0415
    )

    provider = LocalSecretProvider(store_path=tmp_settings.secrets_local_store_path)
    with pytest.raises(KeyError):
        await provider.get("does_not_exist")


def test_secret_provider_dependency__resolves_composition_root_provider(
    tmp_settings: Settings,
) -> None:
    """The HTTP dependency retrieves the protocol implementation from app state."""
    provider = object()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(secret_provider=provider))),
    )

    resolved = get_secret_provider(settings=tmp_settings, request=request)

    assert resolved is provider


def test_secret_provider_dependency__fails_closed_without_composition_root_provider(
    tmp_settings: Settings,
) -> None:
    """A local request cannot silently construct an adapter outside the composition root."""
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))

    with pytest.raises(RuntimeError, match="not configured"):
        get_secret_provider(settings=tmp_settings, request=request)


def test_secret_provider__rejects_azure_when_local_env_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selecting azure_key_vault while the seam is unimplemented fails closed."""

    class _S:
        secret_provider = "azure_key_vault"
        secrets_local_store_path = tmp_path / "x.json"

    with pytest.raises(RuntimeError, match="azure_key_vault"):
        get_secret_provider(_S())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Logging integration smoke (no live HTTP) — confirms adapters are selected
# without raising (AGENTS.md §14 health response contract).
# ---------------------------------------------------------------------------


def test_configure_structlog__is_idempotent() -> None:
    # why: uvicorn workers may import the module multiple times; configure
    # must not raise on repeated calls.
    configure_structlog(level="DEBUG")
    configure_structlog(level="INFO")
    configure_structlog(level="WARNING")
    # why: assert that stdlib logging actually picks up the last level so
    # third-party log lines (uvicorn, sqlalchemy) flow through the same
    # redaction layer.
    assert logging.getLogger().level == logging.WARNING
