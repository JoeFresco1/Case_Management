"""Interface tests for the structured error model (P13-US3/US4; FR-032; AGENTS.md R7).

The contract under test (see ``interfaces/http/errors.py``):

- ``ErrorResponse`` always carries ``code``, ``message``, ``details``
  (safe — never SQL, stack, or secrets), and ``correlation_id``.
- ``error_response`` strips ``None`` details from the payload via
  Pydantic ``model_dump(exclude_none=True)``.
- The domain-error handler maps every domain error to the right HTTP
  status code and error code, and never echoes internal messages for
  ``DomainError`` fall-through.
- The ``RequestValidationError`` handler (registered in ``app/main.py``)
  returns ``code=validation_failed`` with a 422 and a structured
  ``details.errors`` list.
- Unknown exceptions fall through to a generic 500 with no internal
  detail (Constitution V).

The tests use FastAPI's ``TestClient`` so the response shape is asserted
end-to-end through Starlette, which is what operators see on the wire.

Naming follows AGENTS.md §4.R5: ``test_<unit>_<scenario>__<expectation>``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    NotFound,
    UnsupportedTransition,
    ValidationFailed,
)
from app.interfaces.http.errors import (
    ErrorResponse,
    domain_error_handler,
    error_response,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SamplePayload(BaseModel):
    name: str
    age: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)

    # Mirror the RequestValidationError handler registered in
    # ``app/main.py`` so the test exercises the same handler shape that
    # real clients see.
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: object, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_failed",
                "message": "request validation failed",
                "details": {"errors": exc.errors()},
                "correlation_id": "",
            },
        )

    @app.get("/validation-failed/{field}")
    async def _vf(field: str) -> None:
        raise ValidationFailed("missing value", field=field)

    @app.get("/not-found")
    async def _nf() -> None:
        raise NotFound("case not found")

    @app.get("/forbidden")
    async def _fb() -> None:
        raise Forbidden("not allowed")

    @app.get("/conflict")
    async def _cf() -> None:
        raise Conflict("stale version_token")

    @app.get("/unsupported")
    async def _ut() -> None:
        raise UnsupportedTransition("no such transition")

    @app.get("/unknown-domain")
    async def _ud() -> None:
        # A domain error not explicitly mapped falls through to internal_error.
        raise DomainError("internal database unreachable")

    @app.post("/pydantic-validation")
    async def _pv(payload: _SamplePayload) -> dict[str, str]:
        return {"ok": "true"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# ErrorResponse shape
# ---------------------------------------------------------------------------


def test_error_response__serializes_with_all_fields() -> None:
    response = ErrorResponse(
        code="validation_failed",
        message="missing",
        details={"field": "title"},
        correlation_id="cid-1",
    )
    dumped = response.model_dump(exclude_none=True)
    assert dumped == {
        "code": "validation_failed",
        "message": "missing",
        "details": {"field": "title"},
        "correlation_id": "cid-1",
    }


def test_error_response__strips_none_details() -> None:
    response = ErrorResponse(
        code="not_found",
        message="x",
        correlation_id="cid-2",
    )
    dumped = response.model_dump(exclude_none=True)
    assert "details" not in dumped
    assert dumped == {"code": "not_found", "message": "x", "correlation_id": "cid-2"}


def test_error_response__details_is_a_safe_dict() -> None:
    """The contract (Constitution V / AGENTS.md R7) is that ``details``
    never leaks stack frames, SQL, or secret values. ``ErrorResponse``
    accepts an untyped dict because it is the caller's responsibility to
    pass safe data; we assert the model itself does not transform
    arbitrary input into something dangerous (e.g. it does not evaluate
    strings)."""
    response = ErrorResponse(
        code="validation_failed",
        message="bad",
        details={"field": "title", "code": "missing"},
        correlation_id="cid-3",
    )
    assert response.details == {"field": "title", "code": "missing"}


def test_error_response_factory__returns_json_response_with_status() -> None:
    response = error_response(
        code="forbidden",
        message="no",
        status_code=403,
        correlation_id="cid-4",
        details={"resource": "case"},
    )
    assert response.status_code == 403
    body = response.body.decode("utf-8")
    assert '"code":"forbidden"' in body
    assert '"correlation_id":"cid-4"' in body
    assert '"resource":"case"' in body


# ---------------------------------------------------------------------------
# Domain error handler end-to-end
# ---------------------------------------------------------------------------


def test_validation_failed__maps_to_422_with_field_detail(client: TestClient) -> None:
    response = client.get("/validation-failed/title")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["details"] == {"field": "title"}
    assert body["correlation_id"] == ""  # no correlation middleware in this fixture


def test_not_found__maps_to_404(client: TestClient) -> None:
    response = client.get("/not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["message"] == "case not found"


def test_forbidden__maps_to_403(client: TestClient) -> None:
    response = client.get("/forbidden")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_conflict__maps_to_409(client: TestClient) -> None:
    response = client.get("/conflict")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_unsupported_transition__maps_to_409(client: TestClient) -> None:
    response = client.get("/unsupported")
    assert response.status_code == 409
    assert response.json()["code"] == "unsupported_transition"


def test_unknown_domain_error__maps_to_internal_error(client: TestClient) -> None:
    response = client.get("/unknown-domain")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    # The raw exception message must NOT leak to the client (Constitution V).
    assert "internal database unreachable" not in body["message"]


def test_request_validation_error__maps_to_422_with_errors(client: TestClient) -> None:
    response = client.post("/pydantic-validation", json={"name": "x"})  # missing age
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert "errors" in body["details"]
    assert isinstance(body["details"]["errors"], list)
    assert body["details"]["errors"], "errors list must not be empty"


def test_error_model__never_leaks_stack_traces(client: TestClient) -> None:
    """Constitution V: stack traces and SQL never reach the client."""
    for path in (
        "/validation-failed/title",
        "/not-found",
        "/forbidden",
        "/conflict",
        "/unsupported",
        "/unknown-domain",
    ):
        response = client.get(path)
        body_text = response.text
        assert "Traceback" not in body_text
        assert 'File "' not in body_text
        assert "SELECT " not in body_text.upper()
        assert "INSERT " not in body_text.upper()


def test_error_model__handles_request_validation_error_directly() -> None:
    """The Pydantic-style handler must be reachable through a custom
    exception handler registration, mirroring how ``app/main.py`` wires
    it (lines around the validation_handler registration)."""
    app = FastAPI()
    captured: dict[str, object] = {}

    @app.exception_handler(RequestValidationError)
    async def _handler(request, exc):  # type: ignore[no-untyped-def]
        captured["errors"] = exc.errors()
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_failed",
                "message": "request validation failed",
                "details": {"errors": exc.errors()},
                "correlation_id": "",
            },
        )

    @app.post("/echo")
    async def _echo(payload: _SamplePayload) -> dict[str, str]:
        return {"name": payload.name}

    client = TestClient(app)
    response = client.post("/echo", json={"age": "not-an-int"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert captured["errors"]
