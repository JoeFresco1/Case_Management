"""FastAPI app factory and middleware.

Walking-skeleton scope: correlation-id middleware, request log, CORS, error
handlers, and a single mounted API v1 router.
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.application.sla.scheduler import SlaScannerScheduler
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.deps import (
    CorrelationContext,
    SystemClock,
    UserContext,
    clear_local_users,
    register_local_user,
)
from app.domain.errors import DomainError
from app.domain.ports import OutboundDeliveryTransport, SecretProvider
from app.infrastructure.messaging.local_message_bus import (
    LocalInProcessMessageBus,
)
from app.infrastructure.messaging.outbox_dispatcher import OutboxDispatcher
from app.infrastructure.persistence.admin_case_types_http_dependencies import (
    SqlAdminCaseTypesHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_form_bindings_http_dependencies import (
    SqlAdminFormBindingsHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_governance_http_dependencies import (
    SqlAdminGovernanceHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_import_mappings_http_dependencies import (
    SqlAdminImportMappingsHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_outbox_http_dependencies import (
    SqlAdminOutboxHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_references_http_dependencies import (
    SqlAdminReferencesHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_roles_http_dependencies import (
    SqlAdminRolesHttpDependencyProvider,
)
from app.infrastructure.persistence.admin_workflows_http_dependencies import (
    SqlAdminWorkflowsHttpDependencyProvider,
)
from app.infrastructure.persistence.audit_http_dependencies import (
    SqlAuditHttpDependencyProvider,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.capture_http_dependencies import (
    SqlCaptureHttpDependencyProvider,
)
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.case_relationship_http_dependencies import (
    SqlCaseRelationshipHttpDependencyProvider,
)
from app.infrastructure.persistence.channels_http_dependencies import (
    SqlChannelHttpDependencyProvider,
)
from app.infrastructure.persistence.connectors_http_dependencies import (
    SqlConnectorHttpDependencyProvider,
)
from app.infrastructure.persistence.exports_http_dependencies import (
    SqlEvidenceBundleHttpDependencyProvider,
)
from app.infrastructure.persistence.forms_http_dependencies import (
    SqlFormsHttpDependencyProvider,
)
from app.infrastructure.persistence.hold_http_dependencies import (
    SqlHoldHttpDependencyProvider,
)
from app.infrastructure.persistence.idempotency_http_dependencies import (
    SqlIdempotencyHttpDependencyProvider,
)
from app.infrastructure.persistence.imports_http_dependencies import (
    SqlImportHttpDependencyProvider,
)
from app.infrastructure.persistence.migration_http_dependencies import (
    SqlMigrationHttpDependencyProvider,
)
from app.infrastructure.persistence.models import User as UserRow
from app.infrastructure.persistence.participant_http_dependencies import (
    SqlParticipantHttpDependencyProvider,
)
from app.infrastructure.persistence.queue_http_dependencies import (
    SqlQueueHttpDependencyProvider,
)
from app.infrastructure.persistence.reports_http_dependencies import (
    SqlReportsHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import (
    SqlOutboundDeliveryRepository,
    SqlWebhookSubscriptionRepository,
)
from app.infrastructure.persistence.retention_http_dependencies import (
    SqlRetentionHttpDependencyProvider,
)
from app.infrastructure.persistence.search_http_dependencies import (
    SqlDocumentSearchHttpDependencyProvider,
    SqlSearchHttpDependencyProvider,
)
from app.infrastructure.persistence.security_http_dependencies import (
    SqlSecurityHttpDependencyProvider,
)
from app.infrastructure.persistence.siem import InMemorySignedBatchRepository
from app.infrastructure.persistence.sla_scanner import SqlSlaScanner
from app.infrastructure.persistence.viewer_http_dependencies import (
    SqlViewerHttpDependencyProvider,
)
from app.infrastructure.persistence.webhooks_http_dependencies import (
    SqlWebhookHttpDependencyProvider,
)
from app.infrastructure.persistence.worker_home_http_dependencies import (
    SqlWorkerHomeHttpDependencyProvider,
)
from app.infrastructure.secrets.local_secret_provider import LocalSecretProvider
from app.infrastructure.storage.local_document_store import LocalDocumentStore
from app.infrastructure.system.local_system_adapters import (
    LocalDiagnosticsService,
    LocalEncryptionAtRest,
    LocalStorageVolume,
)
from app.infrastructure.telemetry import correlation as telemetry_ctx
from app.infrastructure.telemetry.structured_logging import configure_structlog
from app.infrastructure.webhooks.httpx_transport import HttpXOutboundTransport
from app.interfaces.http.errors import ErrorResponse, domain_error_handler
from app.interfaces.http.idempotency import IdempotencyMiddleware
from app.interfaces.http.routers.admin_case_types import (
    router as admin_case_types_router,
)
from app.interfaces.http.routers.admin_form_bindings import (
    router as admin_form_bindings_router,
)
from app.interfaces.http.routers.admin_governance import (
    router as admin_governance_router,
)
from app.interfaces.http.routers.admin_import_mappings import (
    router as admin_import_mappings_router,
)
from app.interfaces.http.routers.admin_outbox import router as admin_outbox_router
from app.interfaces.http.routers.admin_references import router as admin_references_router
from app.interfaces.http.routers.admin_roles import router as admin_roles_router
from app.interfaces.http.routers.admin_system import (
    router as admin_system_router,
)
from app.interfaces.http.routers.admin_workflows import router as admin_workflows_router
from app.interfaces.http.routers.audit import router as audit_router
from app.interfaces.http.routers.capture import router as capture_router
from app.interfaces.http.routers.case_relations import (
    merges_router as case_relations_merges_router,
)
from app.interfaces.http.routers.case_relations import (
    router as case_relations_router,
)
from app.interfaces.http.routers.case_relations import (
    splits_router as case_relations_splits_router,
)
from app.interfaces.http.routers.case_types import router as case_types_router
from app.interfaces.http.routers.cases import router as cases_router
from app.interfaces.http.routers.channels import router as channels_router
from app.interfaces.http.routers.connectors import router as connectors_router
from app.interfaces.http.routers.doc_bundles import router as doc_bundles_router
from app.interfaces.http.routers.doc_ops import router as doc_ops_router
from app.interfaces.http.routers.document_search import (
    router as document_search_router,
)
from app.interfaces.http.routers.documents import (
    router as documents_router,
)
from app.interfaces.http.routers.documents import (
    versions_router as documents_versions_router,
)
from app.interfaces.http.routers.events import router as events_router
from app.interfaces.http.routers.exports import router as exports_router
from app.interfaces.http.routers.extensions import router as extensions_router
from app.interfaces.http.routers.forms import router as forms_router
from app.interfaces.http.routers.graphql import router as graphql_router
from app.interfaces.http.routers.health import router as health_router
from app.interfaces.http.routers.holds import router as holds_router
from app.interfaces.http.routers.imports import router as imports_router
from app.interfaces.http.routers.participants import router as participants_router
from app.interfaces.http.routers.queues import router as queues_router
from app.interfaces.http.routers.reports import router as reports_router
from app.interfaces.http.routers.retention import router as retention_router
from app.interfaces.http.routers.search import router as search_router
from app.interfaces.http.routers.security import router as security_router
from app.interfaces.http.routers.tasks import router as tasks_router
from app.interfaces.http.routers.viewer import router as viewer_router
from app.interfaces.http.routers.webhooks import router as webhooks_router
from app.interfaces.http.routers.worker_home import router as worker_home_router


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id and HTTP method/path into structlog's
    contextvars for the lifetime of one request, then clear them so the
    next request starts clean (AGENTS.md §18 — contextvars are
    process-global, so leaking across requests would silently misattribute
    log lines).

    The middleware uses :func:`uuid7` for ids it generates so the
    timestamp prefix is sortable — this helps when tailing logs.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Attach request correlation and clear context even when a handler fails."""
        telemetry_ctx.clear()
        cid = request.headers.get("x-correlation-id")
        if cid is None or not cid.strip():
            # Why ``uuid_extensions``: the published package name on
            # PyPI is ``uuid7`` but it ships its module as
            # ``uuid_extensions`` (uuid7 was a placeholder name in
            # early drafts). Importing the actual module name is the
            # only way the dependency installs correctly.
            from uuid_extensions import uuid7  # noqa: PLC0415

            cid = str(uuid7())
        else:
            cid = cid.strip()
        request.state.correlation = CorrelationContext(correlation_id=cid)
        telemetry_ctx.bind_correlation(cid)
        telemetry_ctx.bind_request_metadata(method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
            response.headers["x-correlation-id"] = cid
            return response
        finally:
            # Failed requests must not leave identity or correlation on a reused worker.
            telemetry_ctx.clear()


@lru_cache(maxsize=1)
def _local_secret_provider_cache(store_path: Path) -> LocalSecretProvider:
    """Build one local secret adapter per configured store path.

    The composition root owns concrete adapter construction so interface
    dependencies can retrieve only the ``SecretProvider`` protocol.
    """
    return LocalSecretProvider(store_path=store_path)


def _configure_secret_provider(app: FastAPI, settings: Settings) -> None:
    """Install the configured secret adapter on application state.

    Azure Key Vault remains deliberately unwired in this slice. Leaving the
    state unset preserves the dependency's existing fail-closed error when a
    request attempts to resolve that selector.
    """
    if settings.secret_provider == "local_env":  # noqa: S105
        provider: SecretProvider = _local_secret_provider_cache(settings.secrets_local_store_path)
        app.state.secret_provider = provider


def _install_audit_guards() -> None:
    """Install the ORM-level audit-immutability guards (Layer 2).

    The mapper listener refuses dirty-flush UPDATE/DELETE on
    ``AuditEvent``; the execute listener is a no-op reservation for a
    future portable ``do_orm_execute`` hook (raw SQL is rejected by the
    Layer 1 DB trigger). Idempotent — safe to call once per process.
    """
    from app.db import get_engine  # noqa: PLC0415  (composition-root wiring)
    from app.infrastructure.persistence.audit_events import (  # noqa: PLC0415
        install_audit_immutability_execute_listener,
        install_audit_immutability_listener,
    )

    install_audit_immutability_listener()
    install_audit_immutability_execute_listener(get_engine())


async def _hydrate_local_identity_registry(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Load persisted local users so a separately started server accepts seed keys.

    The seed CLI and API server are separate processes. The in-memory
    registry populated by the CLI therefore cannot be the server's identity
    source; persisted non-system users are reloaded before requests are
    served. A database error propagates so the local API never starts with a
    partial identity registry.
    """
    clear_local_users()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(UserRow)
                .where(UserRow.is_system.is_(False))
                .order_by(UserRow.tenant_id.asc(), UserRow.key.asc())
            )
        ).scalars()
        for row in rows:
            register_local_user(
                UserContext(
                    user_id=row.id,
                    tenant_id=row.tenant_id,
                    display_name=row.display_name,
                    roles=tuple(row.roles or ()),
                ),
                key=row.key,
            )


async def _register_webhook_catalog_subscriber(
    bus: LocalInProcessMessageBus,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    secret_provider: SecretProvider,
    transport: OutboundDeliveryTransport,
) -> None:
    """Register durable outbound delivery creation before the outbox drains.

    Each event gets its own SQL transaction. Database failures propagate to
    the outbox dispatcher, which leaves the source event pending for retry;
    transport failures are recorded on the delivery row for the webhook
    dispatcher to retry with its backoff policy.
    """
    from app.application.webhooks.outbound import (  # noqa: PLC0415
        DeliveryService,
        make_catalog_subscriber,
    )

    async def _receive(event: dict[str, Any]) -> None:
        async with sessionmaker() as session:
            subscriber = make_catalog_subscriber(
                delivery_service_factory=lambda: DeliveryService(
                    delivery_repo=SqlOutboundDeliveryRepository(session),
                    subscription_repo=SqlWebhookSubscriptionRepository(session),
                    transport=transport,
                    secret_provider=secret_provider,
                    clock=SystemClock(),
                ),
                subscription_repo=SqlWebhookSubscriptionRepository(session),
                delivery_repo=SqlOutboundDeliveryRepository(session),
                audit=SqlAuditEventStore(session),
                clock=SystemClock(),
            )
            try:
                await subscriber(event)
                await session.commit()
            except Exception:
                # why: roll back this event's unit of work before surfacing a retryable failure.
                await session.rollback()
                raise

    await bus.subscribe(_receive)


def create_app() -> FastAPI:  # noqa: PLR0915
    settings = get_settings()
    configure_structlog(level=settings.log_level)

    app = FastAPI(
        title="Case Management Platform",
        version="0.1.0",
        description="Local-first, Azure-targeted, modular monolith.",
    )
    # Composition root: concrete SQL adapters stay outside app.interfaces;
    # the router resolves only the infra-free provider contract from state.
    _configure_secret_provider(app, settings)
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.state.queue_http_dependencies = SqlQueueHttpDependencyProvider()
    app.state.document_store = LocalDocumentStore(root=settings.documents_path)
    # T7 diagnostics + system utilities (specs/002-leftovers/spec.md:177):
    # versioned settings store + local volume/encryption/diagnostics fakes.
    # Azure adapters replace these behind the same app.state slots.
    from app.application.configuration.system_settings import (  # noqa: PLC0415
        InMemorySystemSettingsStore,
    )

    app.state.system_settings_store = InMemorySystemSettingsStore()
    app.state.system_diagnostics = LocalDiagnosticsService()
    app.state.storage_volume = LocalStorageVolume(root=settings.documents_path)
    app.state.encryption_at_rest = LocalEncryptionAtRest()
    app.state.idempotency_http_dependencies = SqlIdempotencyHttpDependencyProvider()
    app.state.participant_http_dependencies = SqlParticipantHttpDependencyProvider()
    app.state.admin_case_types_http_dependencies = SqlAdminCaseTypesHttpDependencyProvider()
    app.state.admin_form_bindings_http_dependencies = SqlAdminFormBindingsHttpDependencyProvider()
    app.state.admin_governance_http_dependencies = SqlAdminGovernanceHttpDependencyProvider()
    app.state.admin_roles_http_dependencies = SqlAdminRolesHttpDependencyProvider()
    app.state.admin_references_http_dependencies = SqlAdminReferencesHttpDependencyProvider()
    app.state.admin_workflows_http_dependencies = SqlAdminWorkflowsHttpDependencyProvider()
    app.state.audit_http_dependencies = SqlAuditHttpDependencyProvider()
    app.state.search_http_dependencies = SqlSearchHttpDependencyProvider()
    app.state.document_search_http_dependencies = SqlDocumentSearchHttpDependencyProvider()
    app.state.exports_http_dependencies = SqlEvidenceBundleHttpDependencyProvider()
    app.state.reports_http_dependencies = SqlReportsHttpDependencyProvider()
    app.state.channel_http_dependencies = SqlChannelHttpDependencyProvider()
    app.state.connector_http_dependencies = SqlConnectorHttpDependencyProvider()
    app.state.capture_http_dependencies = SqlCaptureHttpDependencyProvider()
    app.state.webhook_http_dependencies = SqlWebhookHttpDependencyProvider()
    app.state.case_relationship_http_dependencies = SqlCaseRelationshipHttpDependencyProvider()
    app.state.forms_http_dependencies = SqlFormsHttpDependencyProvider()
    app.state.hold_http_dependencies = SqlHoldHttpDependencyProvider()
    app.state.retention_http_dependencies = SqlRetentionHttpDependencyProvider()
    app.state.admin_import_mappings_http_dependencies = (
        SqlAdminImportMappingsHttpDependencyProvider()
    )
    app.state.admin_outbox_http_dependencies = SqlAdminOutboxHttpDependencyProvider()
    app.state.imports_http_dependencies = SqlImportHttpDependencyProvider()
    app.state.migration_http_dependencies = SqlMigrationHttpDependencyProvider()
    app.state.security_http_dependencies = SqlSecurityHttpDependencyProvider()
    # T5 / FM-023 ext: viewer pipeline (annotations, checkout, renditions,
    # re-index). SQL adapters share the request transaction like every
    # other HTTP boundary provider.
    app.state.viewer_http_dependencies = SqlViewerHttpDependencyProvider()
    # T5 worker UX: dashboard + preferences are process-local singletons
    # (synthetic tenants); audit + outbox stay SQL per request so every
    # mutation remains audit-atomic (FR-L02).
    from app.application.worker_home.worker_home import (  # noqa: PLC0415
        InMemoryDashboardStore,
        InMemoryPreferencePolicyStore,
        InMemoryPreferenceStore,
    )

    app.state.worker_home_http_dependencies = SqlWorkerHomeHttpDependencyProvider(
        dashboards=InMemoryDashboardStore(),
        preferences=InMemoryPreferenceStore(),
        policies=InMemoryPreferencePolicyStore(),
    )

    # Append-only audit enforcement, Layer 2 (AGENTS.md §4.R12, FM-003).
    # Installed here — not in ``app.db.get_engine`` — so ``app.db`` stays
    # free of ``app.infrastructure`` imports (T039 layer contract:
    # interfaces -> deps -> db must not reach infrastructure even
    # transitively). Layer 1 is the DB trigger from migration 0002.
    _install_audit_guards()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(IdempotencyMiddleware, scope_paths=("/api/v1/",))
    # The last registered middleware runs first, including on early replay/error responses.
    app.add_middleware(CorrelationMiddleware)

    app.add_exception_handler(DomainError, domain_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        cid = getattr(request.state.correlation, "correlation_id", "")
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                code="validation_failed",
                message="request validation failed",
                details={"errors": exc.errors()},
                correlation_id=cid,
            ).model_dump(exclude_none=True),
        )

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(cases_router, prefix="/api/v1")
    app.include_router(case_types_router, prefix="/api/v1")
    app.include_router(forms_router, prefix="/api/v1")
    app.include_router(participants_router, prefix="/api/v1")
    # P01-US5, US7, US8 / FM-010: case relationships, splits, merges.
    app.include_router(case_relations_router, prefix="/api/v1")
    app.include_router(case_relations_splits_router, prefix="/api/v1")
    app.include_router(case_relations_merges_router, prefix="/api/v1")
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(document_search_router, prefix="/api/v1")
    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(documents_versions_router, prefix="/api/v1")
    # T5 doc ops (x-refs, separation plans, packages, dedup, CSV) +
    # permissioned bundles + share-by-link (T5-US3). Stores are
    # process-local singletons (synthetic tenants; SQL later).
    app.include_router(doc_ops_router, prefix="/api/v1")
    app.include_router(doc_bundles_router, prefix="/api/v1")
    # T5 / FM-023 ext: viewer pipeline (annotations, revisions,
    # renditions, checkout/checkin, re-index) over ``/documents``.
    app.include_router(viewer_router, prefix="/api/v1")
    # T5 worker UX: home dashboard + preferences + admin policy.
    app.include_router(worker_home_router, prefix="/api/v1")
    app.include_router(admin_case_types_router, prefix="/api/v1")
    app.include_router(admin_system_router, prefix="/api/v1")
    app.include_router(admin_form_bindings_router, prefix="/api/v1")
    app.include_router(admin_governance_router, prefix="/api/v1")
    app.include_router(admin_workflows_router, prefix="/api/v1")
    app.include_router(admin_roles_router, prefix="/api/v1")
    app.include_router(admin_import_mappings_router, prefix="/api/v1")
    app.include_router(admin_outbox_router, prefix="/api/v1")
    app.include_router(admin_references_router, prefix="/api/v1")
    app.include_router(retention_router, prefix="/api/v1")
    from app.interfaces.http.routers.retention_evidence import (  # noqa: PLC0415
        router as retention_evidence_router,
    )

    app.include_router(retention_evidence_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(extensions_router, prefix="/api/v1")
    app.include_router(imports_router, prefix="/api/v1")
    # P14-US8 / FM-060: portable case evidence bundle.
    app.include_router(exports_router, prefix="/api/v1")
    app.include_router(channels_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(capture_router, prefix="/api/v1")
    # P13-US10 / FM-056 / ADR-013: outbound webhook surface
    # (``POST /api/v1/webhooks/subscriptions`` + the per-subscription
    # replay endpoint). The inbound surface (``POST /api/v1/webhooks/
    # {channel}``) lives in the same router module. The two are
    # distinct trust surfaces; see ADR-007 vs ADR-013.
    app.include_router(webhooks_router, prefix="/api/v1")
    # P02-US1 / FM-014 / FR-011: task completion and queue read.
    app.include_router(tasks_router, prefix="/api/v1")
    # P02-US3, US9 / FM-016: queue admin + worker claim / release /
    # bulk-handle / delegate. Router exposes the contract; SQL
    # adapters ship with T063/T071/T066-T077 in the follow-up
    # dispatch.
    app.include_router(queues_router, prefix="/api/v1")
    # P11-US8 / P11-US11 / FM-049 / ADR-011: session-control surface
    # (idle / lockout / privileged-action cert). Two endpoints are
    # Gated (approve-client-cert, publish session-policy); the rest
    # are wired behind ``AUDIT_READ`` for the IdP-adapter seam.
    app.include_router(security_router, prefix="/api/v1")
    # P08-US1, US2 / FM-039: legal-hold lifecycle (place / release).
    # The router mounts ``POST /api/v1/cases/{id}/holds`` and
    # ``DELETE /api/v1/cases/{id}/holds``; the use case + the
    # in-memory repository are wired through ``app.state``.
    app.include_router(holds_router, prefix="/api/v1")
    # P12-US1, US2 / FM-051: operational reports (open-cases + overdue-tasks
    # snapshots scoped to the actor's team queue memberships).
    app.include_router(reports_router, prefix="/api/v1")
    # P13-US11 / FM-057: governed GraphQL read surface (policy-enforced
    # resolvers over the application layer; writes stay on REST).
    app.include_router(graphql_router, prefix="/api/v1")

    # FM-046 / P09-US9 / ADR-009 (Gated): the in-memory signed-batch
    # repository satisfies the application port for the synthetic
    # environment. The Azure tamper-evident blob store replaces this
    # once a SIEM target is selected. The port does not change.
    from app.application.documents.doc_ops import (  # noqa: PLC0415
        InMemoryBundleStore,
        InMemoryLinkStore,
        InMemoryXrefStore,
    )

    app.state.xref_store = InMemoryXrefStore()
    app.state.bundle_store = InMemoryBundleStore()
    app.state.link_store = InMemoryLinkStore()

    app.state.signed_batch_repository = InMemorySignedBatchRepository()

    # P08-US1, US2 / FM-039 H1: the SQL legal-hold adapter replaces the
    # H0 in-memory singleton. Holds now survive restarts; the in-memory
    # implementation is reserved for unit tests that do not need a
    # container (AGENTS.md §12 Seams inventory).
    from app.infrastructure.persistence.repositories.legal_hold_repository import (  # noqa: PLC0415
        SqlLegalHoldRepository,
    )

    app.state.legal_hold_repository = SqlLegalHoldRepository(get_sessionmaker())

    # P08-US5, US7, US8 / FM-041: archival + freeze + orphan in-memory
    # adapters. The SQL adapters land with the follow-up dispatch;
    # until then these placeholders keep the HTTP surface wired
    # end-to-end against synthetic tenants.
    from app.infrastructure.persistence.retention_repository import (  # noqa: PLC0415
        InMemoryCaseArchivalRepository,
        InMemoryDisposalFreezeRepository,
        InMemoryOrphanCaseProposalRepository,
    )

    app.state.case_archival_repository = InMemoryCaseArchivalRepository()
    app.state.disposal_freeze_repository = InMemoryDisposalFreezeRepository()
    app.state.orphan_proposal_repository = InMemoryOrphanCaseProposalRepository()

    # Outbox dispatcher loop (FM-055). The task is stored on the app
    # so the shutdown handler can cancel it; the dispatcher is the
    # only integration point between the durable outbox rows and the
    # in-process bus.
    outbox_task: asyncio.Task[None] | None = None
    webhook_task: asyncio.Task[None] | None = None
    sla_scheduler: SlaScannerScheduler | None = None
    bus = LocalInProcessMessageBus()
    webhook_transport = HttpXOutboundTransport()

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal outbox_task, webhook_task, sla_scheduler
        log = structlog.get_logger()
        # Adapter inventory (AGENTS.md §12 + §14 health response): log
        # the active adapter for every seam so operators can verify
        # which adapter is wired without a live /health call.
        log.info(
            "app.startup",
            app_env=settings.app_env,
            synthetic_environment=settings.synthetic_environment,
            adapters={
                # AGENTS.md §12 / docs/integrations/seams.md — every
                # entry corresponds to one of the 8 seams. The local
                # adapter is active by default; the Azure adapters
                # live under app/infrastructure/<seam>/azure_*_placeholder/
                # and are reserved, not wired.
                "db": "local-sqlalchemy",
                "store": "local",
                "bus": "local",
                "idp": "local-header" if settings.app_env == "local" else "entra",
                "secrets": settings.secret_provider,
                "telemetry": settings.telemetry,
                "ai_advisor": "local-noop",
                "enterprise_import": "local-synthetic-fixtures",
                "siem_stream": "local-in-memory",
                "signed_batch_store": "local-in-memory",
            },
        )
        # AGENTS.md §18: the SYNTHETIC ENVIRONMENT marker must be visible
        # in every startup log so operators never mistake a synthetic
        # tenant for a real one. We log at WARNING level (not INFO) so
        # default log pipelines surface it.
        if settings.synthetic_environment:
            log.warning(
                "SYNTHETIC ENVIRONMENT — do not load real data or connect to enterprise systems",
                app_env=settings.app_env,
            )

        if settings.is_local():
            await _hydrate_local_identity_registry(get_sessionmaker())

        # Register before starting the outbox tick: a row dispatched on its
        # first tick must already have the outbound webhook catalog consumer.
        await _register_webhook_catalog_subscriber(
            bus,
            sessionmaker=get_sessionmaker(),
            secret_provider=cast(SecretProvider, getattr(app.state, "secret_provider", None)),
            transport=webhook_transport,
        )

        if settings.outbox_dispatcher_enabled:
            dispatcher = OutboxDispatcher(
                sessionmaker=get_sessionmaker(),
                bus=bus,
                sleep_seconds=float(settings.outbox_dispatch_interval_seconds),
            )
            outbox_task = asyncio.create_task(dispatcher.tick())
            # The admin outbox API needs the dispatcher for ``count_lag``
            # / ``list_pending`` / ``replay``; expose it on ``app.state``
            # so the request handler can resolve it without a global
            # (AGENTS.md §12 — the dispatcher is the integration point).
            app.state.outbox_dispatcher = dispatcher
        else:
            # Even when the tick loop is disabled (single-shot CLI),
            # the admin console still wants to inspect and replay
            # rows — wire a dispatcher that does not auto-tick.
            app.state.outbox_dispatcher = OutboxDispatcher(
                sessionmaker=get_sessionmaker(),
                bus=bus,
                sleep_seconds=float(settings.outbox_dispatch_interval_seconds),
            )

        # P13-US10 / FM-056: outbound webhook delivery loop. Mirrors
        # the outbox pattern: when enabled, a background task drains
        # pending deliveries per tenant (sign → POST → retry →
        # dead-letter); when disabled the dispatcher object is still
        # exposed (non-ticking) so admin read paths keep working.
        if settings.webhook_dispatcher_enabled:
            from app.infrastructure.webhooks.delivery_dispatcher import (  # noqa: PLC0415
                WebhookDeliveryDispatcher,
            )

            webhook_dispatcher = WebhookDeliveryDispatcher(
                sessionmaker=get_sessionmaker(),
                transport=webhook_transport,
                secret_provider=getattr(app.state, "secret_provider", None),
                sleep_seconds=float(settings.webhook_dispatch_interval_seconds),
            )
            webhook_task = asyncio.create_task(webhook_dispatcher.tick())
            app.state.webhook_delivery_dispatcher = webhook_dispatcher

        # INTEGRATION_SEAM: scheduler — local SQL loop; production uses
        # the same scanner contract from an Azure-hosted worker.
        if settings.sla_scanner_enabled:
            scanner = SqlSlaScanner(
                sessionmaker=get_sessionmaker(),
                legal_holds=app.state.legal_hold_repository,
            )
            sla_scheduler = SlaScannerScheduler(
                tenants=scanner.tenant_ids,
                scan_tenant=scanner.scan_tenant,
                interval_seconds=float(settings.sla_scan_interval_seconds),
            )
            sla_scheduler.start()
            app.state.sla_scanner = sla_scheduler

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if sla_scheduler is not None:
            await sla_scheduler.stop()
        if webhook_task is not None:
            webhook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await webhook_task
        if outbox_task is not None:
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
        await webhook_transport.aclose()

    app.state.message_bus = bus
    return app


app = create_app()
