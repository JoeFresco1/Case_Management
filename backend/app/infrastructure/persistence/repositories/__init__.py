"""SQLAlchemy-backed repositories.

Tenant isolation is **explicit**: every method takes ``tenant_id`` and every
query includes ``WHERE tenant_id = :tenant_id``. A failing or absent
second-line listener must not silently allow cross-tenant access.

The module is split per aggregate so each repository has a single
responsibility (Constitution XV / AGENTS.md §4.R2):

- ``case_repository`` — Case aggregate (FR-001 .. FR-007)
- ``case_type_repository`` — CaseType + CaseTypeVersion (FM-008 / FR-015,
  FR-033)
- ``__init__`` re-exports the SQL classes so existing call sites
  (``from app.infrastructure.persistence.repositories import ...``) keep
  working unchanged.

# INTEGRATION_SEAM

Each ``Sql*Repository`` is the local adapter. The future Azure SQL
adapter (``Sql*RepositoryAzure``) implements the same Protocol defined in
``app.domain.ports`` and is wired by ``app.deps`` when the env demands.
"""

from __future__ import annotations

from app.infrastructure.persistence.documents import (
    SqlDocumentRepository,
)
from app.infrastructure.persistence.repositories.break_glass_repository import (
    SqlBreakGlassGrantRepository,
)
from app.infrastructure.persistence.repositories.captured_email_repository import (
    SqlCapturedEmailRepository,
)
from app.infrastructure.persistence.repositories.captured_webhook_repository import (
    SqlCapturedWebhookRepository,
)
from app.infrastructure.persistence.repositories.case_relationship_repository import (
    SqlCaseRelationshipRepository,
)
from app.infrastructure.persistence.repositories.case_repository import (
    SqlCaseRepository,
)
from app.infrastructure.persistence.repositories.case_type_repository import (
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
)
from app.infrastructure.persistence.repositories.channel_repository import (
    SqlChannelRepository,
    SqlQuarantineRepository,
)
from app.infrastructure.persistence.repositories.configuration_publication_repository import (
    SqlConfigurationPublicationRepository,
)
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormBindingRepository,
    SqlFormSessionRepository,
    SqlFormVersionRepository,
)
from app.infrastructure.persistence.repositories.import_mapping_repository import (
    SqlImportMappingRepository,
)
from app.infrastructure.persistence.repositories.legal_hold_repository import (
    SqlLegalHoldRepository,
)
from app.infrastructure.persistence.repositories.outbound_webhook_repository import (
    SqlOutboundDeliveryRepository,
    SqlWebhookSubscriptionRepository,
)
from app.infrastructure.persistence.repositories.participant_repository import (
    SqlParticipantRepository,
)
from app.infrastructure.persistence.repositories.queue_repository import (
    SqlQueueRepository,
)
from app.infrastructure.persistence.repositories.reference_format_repository import (
    SqlReferenceFormatRepository,
)
from app.infrastructure.persistence.repositories.role_repository import (
    SqlRoleRepository,
)
from app.infrastructure.persistence.repositories.saved_search_repository import (
    SqlSavedSearchAuditRepository,
    SqlSavedSearchRepository,
)
from app.infrastructure.persistence.repositories.sla_clock_repository import (
    SqlSlaClockRepository,
)
from app.infrastructure.persistence.repositories.task_repository import (
    SqlTaskRepository,
)
from app.infrastructure.persistence.repositories.workflow_instance_repository import (
    SqlWorkflowInstanceRepository,
    SqlWorkflowTransitionLog,
)
from app.infrastructure.persistence.repositories.workflow_repository import (
    SqlWorkflowDefinitionRepository,
    SqlWorkflowVersionRepository,
)

__all__ = [
    "SqlBreakGlassGrantRepository",
    "SqlCapturedEmailRepository",
    "SqlCapturedWebhookRepository",
    "SqlCaseRelationshipRepository",
    "SqlCaseRepository",
    "SqlCaseTypeRepository",
    "SqlCaseTypeVersionRepository",
    "SqlChannelRepository",
    "SqlConfigurationPublicationRepository",
    "SqlDocumentRepository",
    "SqlFormBindingRepository",
    "SqlFormSessionRepository",
    "SqlFormVersionRepository",
    "SqlImportMappingRepository",
    "SqlLegalHoldRepository",
    "SqlOutboundDeliveryRepository",
    "SqlParticipantRepository",
    "SqlQueueRepository",
    "SqlQuarantineRepository",
    "SqlReferenceFormatRepository",
    "SqlRoleRepository",
    "SqlSavedSearchAuditRepository",
    "SqlSavedSearchRepository",
    "SqlSlaClockRepository",
    "SqlTaskRepository",
    "SqlWebhookSubscriptionRepository",
    "SqlWorkflowDefinitionRepository",
    "SqlWorkflowVersionRepository",
    "SqlWorkflowInstanceRepository",
    "SqlWorkflowTransitionLog",
]
