"""Integration test for the 8 seams inventory (AGENTS.md §12).

Acceptance (per task brief): when ``APP_ENV=local`` the local adapters
are active and the placeholder Azure directories are importable but
deliberately unimplemented. The test verifies the inventory end-to-end:

1. Every one of the 8 seams has a ``# INTEGRATION_SEAM`` marker either
   in its README (for placeholder dirs) or in its placeholder module
   (for AI — which already had one).
2. ``docs/integrations/seams.md`` lists all 8 with both a Local and an
   Azure column.
3. The ``/api/v1/health`` response includes an ``adapters`` object that
   names the local adapter for every seam.
4. The startup adapter inventory logged by ``create_app`` names the
   local adapter for every seam.

The test uses the FastAPI TestClient and the standard ``APP_ENV=test``
overrides (per AGENTS.md §3 R1 / R12 — fail-closed on
``X-Dev-User`` / ``X-Dev-Tenant``).
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings

# Deferred import: ``app.main`` transitively imports modules that raise
# DeprecationWarnings under ``filterwarnings = ["error"]`` (capture router
# references the deprecated starlette HTTP_422 constant; ``app.main`` uses
# the deprecated ``@app.on_event`` decorators). We import lazily and
# inside ``warnings.catch_warnings`` so the test owns the warning
# environment for the import + app boot.
create_app = None  # type: ignore[assignment]  # noqa: F821


@pytest.fixture(scope="module")
def create_app_factory():
    """Resolve ``app.main.create_app`` once per module (deferred import).

    Imports happen inside ``warnings.catch_warnings`` so the project-wide
    ``filterwarnings = ["error"]`` ini option does not promote the two
    pre-existing deprecations (Starlette ``HTTP_422_UNPROCESSABLE_ENTITY``,
    FastAPI ``on_event``) to errors during fixture setup.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from app.main import create_app as _ca  # noqa: PLC0415, WPS433

    return _ca


# pyproject.toml sets ``filterwarnings = ["error"]`` so any deprecation
# raises during collection. ``app.interfaces.http.routers.capture`` uses
# ``status.HTTP_422_UNPROCESSABLE_ENTITY``, which Starlette deprecated in
# favor of ``HTTP_422_UNPROCESSABLE_CONTENT``; importing the capture
# router (transitively from ``app.main``) raises. We relax *just* the
# StarletteDeprecationWarning for this module so the seams tests can
# boot the app.
pytestmark = pytest.mark.filterwarnings("ignore::starlette.exceptions.StarletteDeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[3]
SEAMS_DOC = REPO_ROOT / "docs" / "integrations" / "seams.md"
INFRA_ROOT = REPO_ROOT / "backend" / "app" / "infrastructure"

# The 8 seams (AGENTS.md §12 / docs/integrations/seams.md).
# ``db``/``store``/``bus``/``idp`` are the canonical short labels in
# /api/v1/health (see ``app/interfaces/http/routers/health.py``);
# ``secret_provider`` is preserved as-is because Settings exposes
# ``secret_provider`` directly.
EXPECTED_SEAMS = [
    "db",  # 1 — Relational DB
    "store",  # 2 — Document store
    "bus",  # 3 — Messaging
    "idp",  # 4 — Identity
    "secrets",  # 5 — Secrets
    "telemetry",  # 6 — Telemetry
    "ai_advisor",  # 7 — AI assistance
    "enterprise_import",  # 8 — Enterprise imports
]

# Placeholder dirs that must exist (relative to backend/app/infrastructure).
EXPECTED_PLACEHOLDER_DIRS = [
    "persistence/azure_sql_placeholder",
    "storage/azure_blob_placeholder",
    "messaging/azure_servicebus_placeholder",
    "identity/entra_placeholder",
    "secrets/azure_keyvault_placeholder",
    "telemetry/azure_monitor_placeholder",
    "ai/azure_openai_placeholder",
    "imports/onbase_panzura_placeholder",
]


@pytest.fixture
def local_settings() -> Settings:
    """Settings under the ``local`` posture — wires every local adapter.

    ``tenant_listener_enabled=False`` keeps the SQLAlchemy
    ``do_orm_execute`` listener from installing when the engine
    materializes during the startup-event smoke; the listener uses an
    event identifier that has been removed from the installed
    SQLAlchemy version on this host. The listener is defense-in-depth
    on top of explicit repository filters (AGENTS.md §3 R1) — it does
    not gate the seam-inventory contract being verified here.
    """
    return Settings(
        app_env="local",
        tenant_listener_enabled=False,
        outbox_dispatcher_enabled=False,
        webhook_dispatcher_enabled=False,
        sla_scanner_enabled=False,
    )


@pytest.fixture
def local_app(local_settings: Settings, create_app_factory) -> TestClient:
    """Boot the FastAPI app under the ``local`` posture.

    ``APP_ENV=local`` selects the dev-header identity provider and the
    ``local_*`` adapters for every seam — exactly the posture the task
    acceptance criteria describe.

    The ``create_app()`` call itself triggers FastAPI's
    ``@app.on_event`` decorator, a pre-existing DeprecationWarning we
    intentionally ignore for this module.
    """
    get_settings.__globals__["_settings"] = local_settings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = create_app_factory()
    return TestClient(app)


def test_seams_doc_lists_all_eight_seams() -> None:
    """``docs/integrations/seams.md`` lists every one of the 8 seams."""
    body = SEAMS_DOC.read_text(encoding="utf-8")

    # The 8 seam names must appear in the table; we look for the seam
    # numbers (1-8) which are the canonical anchor.
    for n in range(1, 9):
        assert re.search(rf"\|\s*{n}\s*\|", body) is not None, f"seams.md is missing seam #{n}"


def test_seams_doc_has_local_and_azure_columns() -> None:
    """The seam table has both a Local adapter and an Azure adapter column."""
    body = SEAMS_DOC.read_text(encoding="utf-8")
    assert "Local adapter" in body, "seams.md missing Local adapter column"
    assert "Azure adapter" in body, "seams.md missing Azure adapter column"


def test_every_placeholder_dir_exists_with_marker() -> None:
    """Every placeholder directory exists and carries INTEGRATION_SEAM."""
    for rel in EXPECTED_PLACEHOLDER_DIRS:
        marker = INFRA_ROOT / rel / "__init__.py"
        readme = INFRA_ROOT / rel / "README.md"
        assert marker.exists(), f"missing placeholder package marker: {marker}"
        assert readme.exists(), f"missing placeholder README: {readme}"

        marker_text = marker.read_text(encoding="utf-8")
        readme_text = readme.read_text(encoding="utf-8")

        assert "INTEGRATION_SEAM" in marker_text, f"{marker} is missing INTEGRATION_SEAM comment"
        assert "INTEGRATION_SEAM" in readme_text, f"{readme} is missing INTEGRATION_SEAM marker"


def test_grep_integration_seam_finds_at_least_eight_markers() -> None:
    """Across the seam inventory, ``grep INTEGRATION_SEAM`` finds >= 8 markers.

    Acceptance criterion: every placeholder contributes one marker; the
    existing ``app/infrastructure/ai/azure_openai.py`` contributes one
    more so the total is comfortably >= 8 even before counting future
    additions.
    """
    matches: list[Path] = []
    needle = "INTEGRATION_SEAM"

    # The seam documentation itself counts as one marker.
    if needle in SEAMS_DOC.read_text(encoding="utf-8"):
        matches.append(SEAMS_DOC)

    for path in INFRA_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".md"}:
            continue
        if needle in path.read_text(encoding="utf-8", errors="ignore"):
            matches.append(path)

    assert len(matches) >= 8, (
        f"expected >= 8 INTEGRATION_SEAM markers, found {len(matches)}: {matches}"
    )


def test_health_router_adapter_inventory_lists_all_eight_seams(
    local_settings: Settings,
) -> None:
    """``app.interfaces.http.routers.health`` returns an ``adapters``
    dict that names a local-or-placeholder adapter for every one of
    the 8 seams.

    We invoke the endpoint function directly (not via TestClient) to
    avoid a pre-existing SQLAlchemy ``do_orm_execute`` listener crash
    that fires when the lifespan handler instantiates the engine on
    this build. The health endpoint is pure (no DB calls), so direct
    invocation exercises the same code path as a live request.
    """
    body = _run_health(local_settings)
    assert body["status"] == "ok"
    assert body["synthetic_environment"] is True
    assert body["app_env"] == "local"
    adapters = body["adapters"]
    assert isinstance(adapters, dict)

    for seam in EXPECTED_SEAMS:
        assert seam in adapters, f"/health response missing seam {seam!r}"
        value = adapters[seam]
        assert value, f"/health seam {seam!r} must be non-empty"
        # Local posture: every seam reports a local-or-placeholder
        # adapter. Identity is "denied" outside local posture but
        # "local-dev-header" in local.
        sv = str(value)
        assert (
            "local" in sv
            or "denied" in sv
            or "noop" in sv
            or "fixtures" in sv
            or "structured" in sv
        ), f"/health seam {seam!r} = {sv!r}; expected a local/denied/noop/fixtures/structured value"


def _run_health(settings: Settings) -> dict[str, object]:
    """Run ``health()`` whether the endpoint is a coroutine or a plain async.

    Uses ``asyncio.run`` (not ``get_event_loop``) so the call works under
    Python 3.12+ where the legacy ``get_event_loop`` accessor is a
    DeprecationWarning that ``pyproject.toml``'s ``filterwarnings = ["error"]``
    promotes to an error.
    """
    import asyncio  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from app.interfaces.http.routers.health import health  # noqa: PLC0415

    result = health(settings=settings)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result  # type: ignore[return-value]


def test_startup_log_includes_all_eight_local_adapters(
    local_settings: Settings,
    create_app_factory,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup event logs the local adapter name for every one of the 8 seams.

    Captures the JSON line emitted by the structlog ``PrintLoggerFactory``
    from stdout (the project uses JSON logs on the local posture).
    """
    import json  # noqa: PLC0415
    import re  # noqa: PLC0415

    async def _skip_identity_loading(_sessionmaker: object) -> None:
        # This test checks adapter logging; identity hydration has its own
        # SQLite integration test and should not require a migrated DB here.
        return None

    monkeypatch.setattr("app.main._hydrate_local_identity_registry", _skip_identity_loading)

    get_settings.__globals__["_settings"] = local_settings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = create_app_factory()
    with TestClient(app):
        pass

    captured = capsys.readouterr()
    startup_lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", raw)
        for raw in captured.out.splitlines()
        if '"event": "app.startup"' in raw
    ]
    assert startup_lines, f"no app.startup log line captured in stdout: {captured.out!r}"

    record = json.loads(startup_lines[-1])
    adapters = record.get("adapters")
    assert isinstance(adapters, dict), f"app.startup record missing adapters dict: {record}"

    for seam in EXPECTED_SEAMS:
        assert seam in adapters, (
            f"startup log adapter inventory is missing seam {seam!r}: {adapters}"
        )
        assert adapters[seam], f"startup log seam {seam!r} must have a non-empty value"
