"""Health router — adapter inventory + synthetic marker.

Exposes the runtime posture for every integration seam (AGENTS.md §12,
plan.md:284 — FR-034) so an operator can confirm the active adapters
without tailing logs. The ``synthetic_environment`` field is the
authoritative source for the UI banner (frontend ``shell/Banner.tsx``).

Adapter labels follow the ``<provider>-<flavor>`` convention so the
Azure vs local distinction is unambiguous:

- ``db``: ``local-postgres`` (docker-compose) or ``azure-sql``
- ``store``: ``local-fs`` (content-addressable files under
  ``documents_path``) or ``azure-blob``
- ``bus``: ``local-outbox`` (durable outbox in the relational DB) or
  ``service-bus`` (Azure Service Bus)
- ``idp``: ``local-header`` (X-Dev-User / X-Dev-Tenant, local-only) or
  ``entra`` (Microsoft Entra ID)
- ``secrets``: ``local-env`` or ``azure-key-vault``
- ``telemetry``: ``structured`` (local structlog) or ``otlp``
- ``ai_advisor``: ``local-noop`` (no AI) — ``azure-openai`` is the
  future seam (R11)

Why ``Settings`` is a ``Depends`` parameter rather than a module-level
``get_settings()`` call: the dependency is overridable in tests so a
single health instance can answer truthfully across multiple ``APP_ENV``
postures (R3 / §3 Hard Constraints — never mutate process-global state
to change the runtime posture).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])


def _idp_label(settings: Settings) -> str:
    """Identity-provider posture.

    The local-header IdP is fail-closed (deps.get_current_user raises
    401 for any non-local APP_ENV) so we surface it as ``denied``
    outside ``local`` to make the seam's behaviour visible to an
    operator before a 401 hits their tests.
    """
    if settings.app_env == "local":
        return "local-header"
    return "entra"  # future seam — Azure Entra is the prod target


def _db_label(settings: Settings) -> str:
    """Persistence adapter label.

    Heuristic: any ``postgresql+`` URL is the local docker-compose
    Postgres (the schema uses portable SQLAlchemy generic types per
    §16.1 so the same code runs against Azure SQL in production).
    Anything else — sqlite for tests, asyncpg pointing at an Azure
    Flexible Server, the eventual Azure SQL adapter — is reported
    verbatim with an azure-* suffix where appropriate.
    """
    url = settings.database_url.lower()
    if url.startswith("postgresql+asyncpg://") and "localhost" in url:
        return "local-postgres"
    if "postgres" in url:
        return "local-postgres"  # any other postgres URL is still local
    if "sqlite" in url:
        return "local-sqlite"  # tests only
    if "mssql" in url or "azure" in url:
        return "azure-sql"
    return "azure-sql"


def _store_label(settings: Settings) -> str:  # noqa: ARG001 — seam placeholder
    """Document store adapter label.

    The local adapter writes content-addressable bytes under
    ``settings.documents_path``. The Azure production seam
    (``AzureBlobDocumentStore``) is a future module behind the
    ``DocumentStore`` protocol. Today everything is local-fs — we
    surface azure-blob only when the operator explicitly opts in via
    ``DOCUMENT_STORE=azure-blob``.
    """
    # No setting yet; when added, mirror the toggle here. For now,
    # local is the only adapter implemented (R12 §3 — never
    # silently stub Azure behaviour in domain/application).
    return "local-fs"


def _bus_label(settings: Settings) -> str:  # noqa: ARG001 — seam placeholder
    """Messaging adapter label.

    The local adapter persists events to the durable ``outbox`` table
    in the same transaction as the domain change. The Azure
    production seam (``AzureServiceBus``) is a future module.
    """
    return "local-outbox"


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "synthetic_environment": settings.synthetic_environment,
        "tenant_listener_enabled": settings.tenant_listener_enabled,
        "outbox_dispatcher_enabled": settings.outbox_dispatcher_enabled,
        "adapters": {
            "db": _db_label(settings),
            "store": _store_label(settings),
            "bus": _bus_label(settings),
            "idp": _idp_label(settings),
            "secrets": settings.secret_provider,
            "telemetry": settings.telemetry,
            # AGENTS.md §12 / docs/integrations/seams.md — the local
            # AI-advisor (NoOp) is wired; the Azure OpenAI adapter
            # lives in app/infrastructure/ai/azure_openai_placeholder/.
            "ai_advisor": "local-noop",
            # AGENTS.md §12 — enterprise imports are Gated until
            # Phase 3+; the local posture uses synthetic fixtures via
            # ``app/cli/seed.py``.
            "enterprise_import": "local-synthetic-fixtures",
            # FM-046 / P09-US8, US9 / ADR-009 (Gated): synthetic
            # environment wires the in-memory signed-batch store and
            # the no-op stream sink. The Azure SIEM adapter replaces
            # these when the vendor is selected; the surface stays
            # deny-by-default until a role grants ``audit.stream``.
            "siem_stream": "local-in-memory",
            "signed_batch_store": "local-in-memory",
        },
    }


__all__ = ["router"]
