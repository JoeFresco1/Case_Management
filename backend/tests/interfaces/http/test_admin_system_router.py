"""HTTP acceptance tests for the system-configuration surface (T7 / P10).

Covers ``/api/v1/admin/system`` mounted in ``app.main``:

- ``GET /settings`` lists redacted versioned settings (defaults seeded).
- ``PUT /settings/{key}`` validates + appends versions; stale tokens
  are 409 ``conflict``; malformed values are 422 ``validation_failed``.
- ``GET /diagnostics`` reports cache stats + desired/actual services.
- ``POST /cache/clear`` clears the diagnostics cache (audited).
- ``GET /storage`` shows declared config vs actual local adapters.
- Every path requires the System Configuration right
  (``ADMIN_TENANT_CONFIG``): other roles get the 403 ``forbidden``
  envelope; tenant B never sees tenant A's versions.
- Azure-free: wired against the local adapters only (no Azure SDK).

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Structure is ``// given / // when / // then``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.configuration.system_settings import (
    InMemorySystemSettingsStore,
)
from app.deps import UserContext, get_current_user
from app.domain.errors import DomainError
from app.infrastructure.system.local_system_adapters import (
    LocalDiagnosticsService,
    LocalEncryptionAtRest,
    LocalStorageVolume,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_system import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _build_app(
    store: InMemorySystemSettingsStore,
    *,
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("process_admin",),
    wire_adapters: bool = True,
    tmp_path: Path | None = None,
) -> FastAPI:
    """Build a minimal app with the system router + local fakes."""
    # // given the composition-root wiring (local adapters only, no Azure)
    app = FastAPI()
    app.state.system_settings_store = store
    if wire_adapters:
        root = tmp_path if tmp_path is not None else Path("./documents")
        app.state.system_diagnostics = LocalDiagnosticsService()
        app.state.storage_volume = LocalStorageVolume(root=root)
        app.state.encryption_at_rest = LocalEncryptionAtRest()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic Admin",
        roles=roles,
    )
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_list_settings__process_admin__returns_redacted_defaults() -> None:
    """An admin listing settings gets seeded redacted defaults at version 1."""
    # // given the wired app for tenant A
    app = _build_app(InMemorySystemSettingsStore())
    # // when listing settings
    with _client(app) as client:
        response = client.get("/api/v1/admin/system/settings")
    # // then 200 with the four T7 surfaces present, all version 1
    assert response.status_code == 200, response.text
    items = {row["key"]: row for row in response.json()["items"]}
    assert "logging.cases" in items
    assert "diagnostics.cache" in items
    assert "storage.volume" in items
    assert "encryption.at_rest" in items
    assert items["logging.cases"]["version"] == 1
    assert items["logging.cases"]["value"]["level"] == "INFO"
    assert items["encryption.at_rest"]["value"] == {"enabled": False, "key_ref": ""}


def test_list_settings__intake_role__returns_403_forbidden() -> None:
    """The System Configuration right denies non-admin roles (fail-closed)."""
    # // given the app authenticated as intake (no admin grant)
    app = _build_app(InMemorySystemSettingsStore(), roles=("intake",))
    # // when listing settings
    with _client(app) as client:
        response = client.get("/api/v1/admin/system/settings")
    # // then the structured forbidden envelope (no data leak)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_update_setting__valid_log_level__returns_version_2_audit_event() -> None:
    """A valid module-log update returns version 2 + the audit event type."""
    # // given the wired app
    app = _build_app(InMemorySystemSettingsStore())
    with _client(app) as client:
        # // when updating the log level (expected_version=1)
        response = client.put(
            "/api/v1/admin/system/settings/logging.cases",
            json={
                "value": {"level": "DEBUG", "sink": "none", "rotation": "daily"},
                "expected_version": 1,
                "change_summary": "verbose triage",
            },
        )
    # // then version 2 with the audited event marker
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["value"]["level"] == "DEBUG"
    assert body["audit_event"] == "SystemSettingUpdated"


def test_update_setting__stale_version__returns_409_conflict() -> None:
    """A concurrent edit with a stale token is 409 (no silent overwrite)."""
    # // given version 2 of diagnostics.cache
    store = InMemorySystemSettingsStore()
    app = _build_app(store)
    with _client(app) as client:
        first = client.put(
            "/api/v1/admin/system/settings/diagnostics.cache",
            json={
                "value": {"max_entries": 500, "ttl_seconds": 60},
                "expected_version": 1,
                "change_summary": "first",
            },
        )
        assert first.status_code == 200, first.text
        # // when replaying expected_version=1
        second = client.put(
            "/api/v1/admin/system/settings/diagnostics.cache",
            json={
                "value": {"max_entries": 700, "ttl_seconds": 60},
                "expected_version": 1,
                "change_summary": "stale",
            },
        )
    # // then the structured conflict envelope
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "conflict"


def test_update_setting__invalid_level__returns_422_validation_failed() -> None:
    """A malformed module-log value is 422 (no partial write)."""
    # // given the wired app
    app = _build_app(InMemorySystemSettingsStore())
    # // when writing an invalid level
    with _client(app) as client:
        response = client.put(
            "/api/v1/admin/system/settings/logging.cases",
            json={
                "value": {"level": "VERBOSE", "sink": "none", "rotation": "daily"},
                "expected_version": 1,
                "change_summary": "",
            },
        )
    # // then validation_failed
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_update_setting__secret_like_key__redacted_in_response() -> None:
    """Secret-like keys never render verbatim, even to admins (FM-011)."""
    # // given the wired app
    app = _build_app(InMemorySystemSettingsStore())
    # // when storing a value that embeds a secret-like key
    with _client(app) as client:
        response = client.put(
            "/api/v1/admin/system/settings/logging.audit",
            json={
                "value": {
                    "level": "INFO",
                    "sink": "splunk",
                    "rotation": "daily",
                    "api_key": "hunter2",
                },
                "expected_version": 1,
                "change_summary": "",
            },
        )
    # // then redacted in the mutation response and on re-read
    assert response.status_code == 200, response.text
    assert response.json()["value"]["api_key"] == "[REDACTED]"
    with _client(app) as client:
        reread = client.get("/api/v1/admin/system/settings/logging.audit")
    assert reread.json()["value"]["api_key"] == "[REDACTED]"


def test_get_setting__unknown_key__returns_404_not_found() -> None:
    """An undeclared key is 404 (tenant-scoped, no empty default)."""
    # // given the wired app
    app = _build_app(InMemorySystemSettingsStore())
    # // when reading an unknown key
    with _client(app) as client:
        response = client.get("/api/v1/admin/system/settings/storage.tape")
    # // then not_found
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_diagnostics__wired_adapters__reports_cache_and_services(
    tmp_path: Path,
) -> None:
    """Diagnostics reports cache stats + desired vs actual services."""
    # // given the wired app with a primed diagnostics cache
    store = InMemorySystemSettingsStore()
    app = _build_app(store, tmp_path=tmp_path)
    app.state.system_diagnostics.put("probe", {"ok": True})
    # // when reading diagnostics
    with _client(app) as client:
        response = client.get("/api/v1/admin/system/diagnostics")
    # // then cache stats + desired tenant config + actual process flags
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cache"]["entries"] == 1
    assert body["services_desired"]["outbox"] is True
    assert body["services_actual"]["outbox"] is True
    assert body["synthetic_environment"] is True


def test_cache_clear__primed_cache__returns_cleared_count() -> None:
    """The cache utility clears entries and reports the count (audited)."""
    # // given the wired app with two cached entries
    store = InMemorySystemSettingsStore()
    app = _build_app(store)
    app.state.system_diagnostics.put("a", 1)
    app.state.system_diagnostics.put("b", 2)
    # // when clearing
    with _client(app) as client:
        response = client.post("/api/v1/admin/system/cache/clear")
    # // then the cleared count; a second clear is 0 (idempotent utility)
    assert response.status_code == 200, response.text
    assert response.json()["cleared"] == 2
    with _client(app) as client:
        again = client.post("/api/v1/admin/system/cache/clear")
    assert again.json()["cleared"] == 0


def test_storage__wired_adapters__declared_vs_actual_local(
    tmp_path: Path,
) -> None:
    """Storage shows declared config vs actual local-fs/plaintext adapters."""
    # // given the wired app
    app = _build_app(InMemorySystemSettingsStore(), tmp_path=tmp_path)
    # // when reading the storage view
    with _client(app) as client:
        response = client.get("/api/v1/admin/system/storage")
    # // then declared config + actual infrastructure-only choices
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["declared_volume"]["adapter"] == "local-fs"
    assert body["declared_encryption"]["enabled"] is False
    assert body["actual_volume"]["adapter"] == "local-fs"
    assert body["actual_encryption"]["adapter"] == "local-plaintext"


def test_storage__unwired_adapters__degrades_to_settings_view() -> None:
    """Without composition-root wiring the view degrades (never 500s)."""
    # // given a minimal app (router only, no adapters on state)
    app = _build_app(InMemorySystemSettingsStore(), wire_adapters=False)
    # // when reading storage + diagnostics
    with _client(app) as client:
        storage = client.get("/api/v1/admin/system/storage")
        diagnostics = client.get("/api/v1/admin/system/diagnostics")
        cleared = client.post("/api/v1/admin/system/cache/clear")
    # // then settings-derived views with zeroed cache utility
    assert storage.status_code == 200, storage.text
    assert storage.json()["actual_volume"]["adapter"] == "local-fs"
    assert diagnostics.status_code == 200, diagnostics.text
    assert cleared.json()["cleared"] == 0


def test_settings__tenant_b__sees_defaults_not_tenant_a_versions() -> None:
    """Tenant isolation holds end-to-end (R10: two-tenant test)."""
    # // given tenant A customized logging.cases to version 2
    store = InMemorySystemSettingsStore()
    app_a = _build_app(store)
    with _client(app_a) as client:
        updated = client.put(
            "/api/v1/admin/system/settings/logging.cases",
            json={
                "value": {"level": "DEBUG", "sink": "none", "rotation": "daily"},
                "expected_version": 1,
                "change_summary": "tenant A only",
            },
        )
        assert updated.status_code == 200, updated.text
    # // when tenant B (different admin) lists settings
    admin_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    app_b = _build_app(store, user_id=admin_b, tenant_id=TENANT_B)
    with _client(app_b) as client:
        response = client.get("/api/v1/admin/system/settings")
    # // then tenant B sees version-1 defaults (INFO), not A's DEBUG
    assert response.status_code == 200, response.text
    items = {row["key"]: row for row in response.json()["items"]}
    assert items["logging.cases"]["version"] == 1
    assert items["logging.cases"]["value"]["level"] == "INFO"
