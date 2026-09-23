"""Infrastructure-free FastAPI dependency resolvers.

Concrete providers are placed on ``app.state`` by the composition root. This
module only retrieves the provider contract, which keeps HTTP modules free of
infrastructure imports and leaves ``dependency_overrides`` available to tests.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.application.capture.http_dependencies import CaptureHttpDependencyProvider
from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.cases.relationship_http_dependencies import (
    CaseRelationshipHttpDependencyProvider,
)
from app.application.channels.http_dependencies import ChannelHttpDependencyProvider
from app.application.configuration.http_dependencies import (
    AdminCaseTypesHttpDependencyProvider,
    AdminGovernanceHttpDependencyProvider,
)
from app.application.connectors.http_dependencies import ConnectorHttpDependencyProvider
from app.application.documents.http_dependencies import ViewerHttpDependencyProvider
from app.application.exports.http_dependencies import EvidenceBundleHttpDependencyProvider
from app.application.forms.admin.http_dependencies import (
    AdminFormBindingsHttpDependencyProvider,
)
from app.application.forms.http_dependencies import FormsHttpDependencyProvider
from app.application.holds.http_dependencies import HoldHttpDependencyProvider
from app.application.imports.admin.http_dependencies import (
    AdminImportMappingsHttpDependencyProvider,
)
from app.application.imports.http_dependencies import ImportHttpDependencyProvider
from app.application.migration.http_dependencies import MigrationHttpDependencyProvider
from app.application.outbox.http_dependencies import AdminOutboxHttpDependencyProvider
from app.application.participants.http_dependencies import ParticipantHttpDependencyProvider
from app.application.queues.http_dependencies import QueueHttpDependencyProvider
from app.application.references.http_dependencies import (
    AdminReferencesHttpDependencyProvider,
)
from app.application.reports.http_dependencies import ReportsHttpDependencyProvider
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.application.roles.http_dependencies import AdminRolesHttpDependencyProvider
from app.application.search.http_dependencies import (
    DocumentSearchHttpDependencyProvider,
    SearchHttpDependencyProvider,
)
from app.application.security.http_dependencies import SecurityHttpDependencyProvider
from app.application.webhooks.http_dependencies import WebhookHttpDependencyProvider
from app.application.workflow.http_dependencies import (
    AdminWorkflowsHttpDependencyProvider,
)


def get_case_dependencies(request: Request) -> CaseHttpDependencyProvider:
    """Resolve the case provider installed by the application composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "case_http_dependencies", None)
    if provider is None:
        raise RuntimeError("case HTTP dependencies are not configured")
    return cast(CaseHttpDependencyProvider, provider)


def get_participant_dependencies(request: Request) -> ParticipantHttpDependencyProvider:
    """Resolve the participant provider installed by the composition root."""
    provider = getattr(request.app.state, "participant_http_dependencies", None)
    if provider is None:
        raise RuntimeError("participant HTTP dependencies are not configured")
    return cast(ParticipantHttpDependencyProvider, provider)


def get_audit_dependencies(request: Request) -> AuditHttpDependencyProvider:
    """Resolve the audit provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "audit_http_dependencies", None)
    if provider is None:
        raise RuntimeError("audit HTTP dependencies are not configured")
    return cast(AuditHttpDependencyProvider, provider)


def get_admin_case_types_dependencies(request: Request) -> AdminCaseTypesHttpDependencyProvider:
    """Resolve the admin CaseType provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "admin_case_types_http_dependencies", None)
    if provider is None:
        raise RuntimeError("admin CaseType HTTP dependencies are not configured")
    return cast(AdminCaseTypesHttpDependencyProvider, provider)


def get_admin_governance_dependencies(request: Request) -> AdminGovernanceHttpDependencyProvider:
    """Resolve the admin governance provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "admin_governance_http_dependencies", None)
    if provider is None:
        raise RuntimeError("admin governance HTTP dependencies are not configured")
    return cast(AdminGovernanceHttpDependencyProvider, provider)


def get_admin_workflows_dependencies(request: Request) -> AdminWorkflowsHttpDependencyProvider:
    """Resolve the admin workflow provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "admin_workflows_http_dependencies", None)
    if provider is None:
        raise RuntimeError("admin workflow HTTP dependencies are not configured")
    return cast(AdminWorkflowsHttpDependencyProvider, provider)


def get_admin_roles_dependencies(request: Request) -> AdminRolesHttpDependencyProvider:
    """Resolve the admin role provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "admin_roles_http_dependencies", None)
    if provider is None:
        raise RuntimeError("admin role HTTP dependencies are not configured")
    return cast(AdminRolesHttpDependencyProvider, provider)


def get_admin_references_dependencies(request: Request) -> AdminReferencesHttpDependencyProvider:
    """Resolve the admin reference-format provider installed by the composition root.

    Raises:
        RuntimeError: if an app was created without the required provider.
    """
    provider = getattr(request.app.state, "admin_references_http_dependencies", None)
    if provider is None:
        raise RuntimeError("admin reference-format HTTP dependencies are not configured")
    return cast(AdminReferencesHttpDependencyProvider, provider)


def _provider_or_raise(request: Request, attr: str, missing_message: str) -> object:
    """Read a provider off ``app.state``, failing closed when unwired."""
    provider = getattr(request.app.state, attr, None)
    if provider is None:
        raise RuntimeError(missing_message)
    return provider


def get_search_dependencies(request: Request) -> SearchHttpDependencyProvider:
    """Resolve the search provider installed by the composition root."""
    return cast(
        SearchHttpDependencyProvider,
        _provider_or_raise(
            request,
            "search_http_dependencies",
            "search HTTP dependencies are not configured",
        ),
    )


def get_document_search_dependencies(request: Request) -> DocumentSearchHttpDependencyProvider:
    """Resolve the document-search provider installed by the composition root."""
    return cast(
        DocumentSearchHttpDependencyProvider,
        _provider_or_raise(
            request,
            "document_search_http_dependencies",
            "document-search HTTP dependencies are not configured",
        ),
    )


def get_exports_dependencies(request: Request) -> EvidenceBundleHttpDependencyProvider:
    """Resolve the exports provider installed by the composition root."""
    return cast(
        EvidenceBundleHttpDependencyProvider,
        _provider_or_raise(
            request,
            "exports_http_dependencies",
            "exports HTTP dependencies are not configured",
        ),
    )


def get_reports_dependencies(request: Request) -> ReportsHttpDependencyProvider:
    """Resolve the reports provider installed by the composition root."""
    return cast(
        ReportsHttpDependencyProvider,
        _provider_or_raise(
            request,
            "reports_http_dependencies",
            "reports HTTP dependencies are not configured",
        ),
    )


def get_channel_dependencies(request: Request) -> ChannelHttpDependencyProvider:
    """Resolve the channels provider installed by the composition root."""
    return cast(
        ChannelHttpDependencyProvider,
        _provider_or_raise(
            request,
            "channel_http_dependencies",
            "channel HTTP dependencies are not configured",
        ),
    )


def get_connector_dependencies(request: Request) -> ConnectorHttpDependencyProvider:
    """Resolve the connectors provider installed by the composition root."""
    return cast(
        ConnectorHttpDependencyProvider,
        _provider_or_raise(
            request,
            "connector_http_dependencies",
            "connector HTTP dependencies are not configured",
        ),
    )


def get_capture_dependencies(request: Request) -> CaptureHttpDependencyProvider:
    """Resolve the capture provider installed by the composition root."""
    return cast(
        CaptureHttpDependencyProvider,
        _provider_or_raise(
            request,
            "capture_http_dependencies",
            "capture HTTP dependencies are not configured",
        ),
    )


def get_webhook_dependencies(request: Request) -> WebhookHttpDependencyProvider:
    """Resolve the webhooks provider installed by the composition root."""
    return cast(
        WebhookHttpDependencyProvider,
        _provider_or_raise(
            request,
            "webhook_http_dependencies",
            "webhook HTTP dependencies are not configured",
        ),
    )


def get_case_relationship_dependencies(request: Request) -> CaseRelationshipHttpDependencyProvider:
    """Resolve the case-relationships provider installed by the composition root."""
    return cast(
        CaseRelationshipHttpDependencyProvider,
        _provider_or_raise(
            request,
            "case_relationship_http_dependencies",
            "case-relationship HTTP dependencies are not configured",
        ),
    )


def get_forms_dependencies(request: Request) -> FormsHttpDependencyProvider:
    """Resolve the forms provider installed by the composition root."""
    return cast(
        FormsHttpDependencyProvider,
        _provider_or_raise(
            request,
            "forms_http_dependencies",
            "forms HTTP dependencies are not configured",
        ),
    )


def get_queue_dependencies(request: Request) -> QueueHttpDependencyProvider:
    """Resolve the queue provider installed by the composition root."""
    return cast(
        QueueHttpDependencyProvider,
        _provider_or_raise(
            request,
            "queue_http_dependencies",
            "queue HTTP dependencies are not configured",
        ),
    )


def get_hold_dependencies(request: Request) -> HoldHttpDependencyProvider:
    """Resolve the holds provider installed by the composition root."""
    return cast(
        HoldHttpDependencyProvider,
        _provider_or_raise(
            request,
            "hold_http_dependencies",
            "hold HTTP dependencies are not configured",
        ),
    )


def get_retention_dependencies(request: Request) -> RetentionHttpDependencyProvider:
    """Resolve the retention provider installed by the composition root."""
    return cast(
        RetentionHttpDependencyProvider,
        _provider_or_raise(
            request,
            "retention_http_dependencies",
            "retention HTTP dependencies are not configured",
        ),
    )


def get_admin_outbox_dependencies(request: Request) -> AdminOutboxHttpDependencyProvider:
    """Resolve the admin-outbox provider installed by the composition root."""
    return cast(
        AdminOutboxHttpDependencyProvider,
        _provider_or_raise(
            request,
            "admin_outbox_http_dependencies",
            "admin-outbox HTTP dependencies are not configured",
        ),
    )


def get_imports_dependencies(request: Request) -> ImportHttpDependencyProvider:
    """Resolve the imports provider installed by the composition root."""
    return cast(
        ImportHttpDependencyProvider,
        _provider_or_raise(
            request,
            "imports_http_dependencies",
            "imports HTTP dependencies are not configured",
        ),
    )


def get_migration_dependencies(request: Request) -> MigrationHttpDependencyProvider:
    """Resolve the migration provider installed by the composition root."""
    return cast(
        MigrationHttpDependencyProvider,
        _provider_or_raise(
            request,
            "migration_http_dependencies",
            "migration HTTP dependencies are not configured",
        ),
    )


def get_admin_form_bindings_dependencies(
    request: Request,
) -> AdminFormBindingsHttpDependencyProvider:
    """Resolve the admin form-binding provider installed by the composition root."""
    return cast(
        AdminFormBindingsHttpDependencyProvider,
        _provider_or_raise(
            request,
            "admin_form_bindings_http_dependencies",
            "admin form-binding HTTP dependencies are not configured",
        ),
    )


def get_admin_import_mappings_dependencies(
    request: Request,
) -> AdminImportMappingsHttpDependencyProvider:
    """Resolve the admin import-mapping provider installed by the composition root."""
    return cast(
        AdminImportMappingsHttpDependencyProvider,
        _provider_or_raise(
            request,
            "admin_import_mappings_http_dependencies",
            "admin import-mapping HTTP dependencies are not configured",
        ),
    )


def get_security_dependencies(request: Request) -> SecurityHttpDependencyProvider:
    """Resolve the security provider installed by the composition root."""
    return cast(
        SecurityHttpDependencyProvider,
        _provider_or_raise(
            request,
            "security_http_dependencies",
            "security HTTP dependencies are not configured",
        ),
    )


def get_viewer_dependencies(request: Request) -> ViewerHttpDependencyProvider:
    """Resolve the viewer provider installed by the composition root."""
    return cast(
        ViewerHttpDependencyProvider,
        _provider_or_raise(
            request,
            "viewer_http_dependencies",
            "viewer HTTP dependencies are not configured",
        ),
    )


__all__ = [
    "get_admin_case_types_dependencies",
    "get_admin_form_bindings_dependencies",
    "get_admin_governance_dependencies",
    "get_admin_import_mappings_dependencies",
    "get_admin_outbox_dependencies",
    "get_admin_roles_dependencies",
    "get_admin_references_dependencies",
    "get_admin_workflows_dependencies",
    "get_audit_dependencies",
    "get_capture_dependencies",
    "get_case_dependencies",
    "get_case_relationship_dependencies",
    "get_channel_dependencies",
    "get_connector_dependencies",
    "get_document_search_dependencies",
    "get_exports_dependencies",
    "get_forms_dependencies",
    "get_hold_dependencies",
    "get_imports_dependencies",
    "get_migration_dependencies",
    "get_participant_dependencies",
    "get_queue_dependencies",
    "get_reports_dependencies",
    "get_retention_dependencies",
    "get_search_dependencies",
    "get_security_dependencies",
    "get_viewer_dependencies",
    "get_webhook_dependencies",
]
