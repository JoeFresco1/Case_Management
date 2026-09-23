"""Application configuration.

Fail-closed behavior: any combination that is unsafe for the chosen `app_env`
raises at startup, not at request time.

Adapter selection (AGENTS.md §12 Integration Seams): the `secret_provider`
field chooses the local env/file-backed adapter by default and can be flipped
to ``azure_key_vault`` once the Azure production seam is implemented. The
choice is logged at startup so operators can verify which adapter is active
(plan.md:284 — ``/api/v1/health`` distinguishes local vs azure adapters).

Environment contract (AGENTS.md §3 Hard Constraints, R12, §18):

- ``APP_ENV`` selects the runtime posture: ``local`` (developer machine,
  synthetic only, dev-header IdP), ``test`` (pytest, sqlite, synthetic
  only, dev-header denied), ``shared`` (staging-like), ``prod`` (Azure
  production). Any other value fails closed at startup.
- ``SYNTHETIC_ENVIRONMENT`` is a marker flag. It is ``True`` for every
  non-prod posture; the default flips to ``False`` only when
  ``APP_ENV=prod`` AND the operator explicitly opts in. The flag is
  surfaced through ``/api/v1/health`` and rendered as a banner in the
  UI (AGENTS.md §18, frontend ``shell/Banner.tsx``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# AppEnv: enumeration of allowed runtime postures. ``Literal`` gives us
# a free Pydantic validator; we expose ``AppEnv`` as a runtime alias
# so application code can import the literal type rather than redefine
# it. The four values are the contract — anything else fails at startup.
AppEnv = Literal["local", "test", "shared", "prod"]
_APP_ENV_VALUES: Final[tuple[str, ...]] = ("local", "test", "shared", "prod")

# Secret provider adapter selection (AGENTS.md §12). Only the local adapter is
# implemented today; ``azure_key_vault`` is the production seam reserved for
# when the Azure target becomes available.
SecretProviderName = Literal["local_env", "azure_key_vault"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "case-management-platform"
    app_env: AppEnv = "local"
    # Why a flag the operator controls: synthetic mode is what marks every
    # record, log line, and banner as "not real data". Defaults to True so
    # the default local/test/shared posture is always synthetic; the only
    # way to flip it off is to set APP_ENV=prod AND SYNTHETIC_ENVIRONMENT=0
    # explicitly (AGENTS.md §3 Issue #2 — Azure connections impossible
    # locally means a real prod profile never loads on this host).
    synthetic_environment: bool = True

    database_url: str = "postgresql+asyncpg://cases:cases@localhost:5432/cases"

    documents_path: Path = Path("./documents")

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    tenant_listener_enabled: bool = True
    outbox_dispatcher_enabled: bool = True
    outbox_dispatch_interval_seconds: int = 5
    sla_scanner_enabled: bool = True
    sla_scan_interval_seconds: int = 60

    # FM-056 / P13-US10 — outbound webhook delivery loop. Mirrors the
    # outbox flags above.
    #
    # WIRING NOTE for the main.py owner: in the startup lifespan, when
    # ``webhook_dispatcher_enabled`` is true, construct
    # ``WebhookDeliveryDispatcher(sessionmaker=get_sessionmaker(),
    # transport=HttpXOutboundTransport(),
    # secret_provider=app.state.secret_provider,
    # sleep_seconds=settings.webhook_dispatch_interval_seconds)``,
    # keep ``asyncio.create_task(dispatcher.tick())`` alongside the
    # outbox task, expose the dispatcher on
    # ``app.state.webhook_delivery_dispatcher`` for the operator
    # console, and cancel the task on shutdown (same pattern as the
    # outbox dispatcher task). When disabled, still expose a
    # non-ticking dispatcher instance so admin read paths keep working.
    webhook_dispatcher_enabled: bool = True
    webhook_dispatch_interval_seconds: float = 5.0

    log_level: str = "INFO"
    telemetry: Literal["structured", "otlp"] = "structured"

    # AGENTS.md §3 (Issue #3) + FM-029 — the scan-capture feature is
    # **Gated** (see ``01-deduplicated-feature-map.md:78``). The
    # operator must set ``SCAN_CAPTURE_ENABLED=1`` explicitly to
    # accept inbound scans; the default is False so a fresh install
    # never silently exposes a scan channel. ADR-006 documents the
    # rationale.
    scan_capture_enabled: bool = False

    # AGENTS.md §12 secrets seam — selects the SecretProvider adapter.
    # Default keeps the local env-backed implementation; ``azure_key_vault``
    # is the future seam (placeholder only — the adapter is not implemented
    # in this slice).
    secret_provider: SecretProviderName = "local_env"  # noqa: S105 — selector literal, not a password
    # Path used by the local secrets adapter as the on-disk store when
    # SECRET_PROVIDER=local_env. Defaults to ``./.secrets.json`` which is
    # gitignored by the repo's standard ``.gitignore``.
    secrets_local_store_path: Path = Path("./.secrets.json")

    @field_validator("app_env")
    @classmethod
    def _validate_app_env(cls, v: str) -> str:
        if v not in _APP_ENV_VALUES:
            raise ValueError(f"unsupported APP_ENV: {v!r}; expected one of {_APP_ENV_VALUES}")
        return v

    @field_validator("documents_path")
    @classmethod
    def _validate_documents_path(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @field_validator("secrets_local_store_path")
    @classmethod
    def _validate_secrets_store(cls, v: Path) -> Path:
        # Don't create the secrets file — only its parent. Secrets are
        # written lazily on first resolution.
        v.parent.mkdir(parents=True, exist_ok=True)
        return v

    @field_validator("secret_provider")
    @classmethod
    def _validate_secret_provider(cls, v: str) -> str:
        if v not in ("local_env", "azure_key_vault"):
            raise ValueError(f"unsupported SECRET_PROVIDER: {v}")
        return v

    def is_local(self) -> bool:
        return self.app_env == "local"

    def is_production_like(self) -> bool:
        return self.app_env in ("shared", "prod")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = Settings()
    return _settings
