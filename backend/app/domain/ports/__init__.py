"""Domain ports package -- infrastructure abstractions (Protocols).

The domain layer depends on these abstractions only. Adapters in
``app.infrastructure.*`` implement them. Swapping a local adapter for an
Azure adapter requires no domain or application changes.

This package splits the monolithic ``ports.py`` into focused modules:
- ``core``: Case, CaseType, Document, Tenant (foundational aggregates)
- ``workflow``: WorkflowDefinition, WorkflowVersion
- ``task_queue``: Task, Queue, WorkflowInstance, WorkflowTransitionLog
- ``sla``: SlaClock
- ``audit``: AuditEventStore
- ``outbox_idempotency``: Outbox, Idempotency
- ``infrastructure``: DocumentStore, MessageBus, SecretProvider, Telemetry
- ``role_session``: Role, Session, SessionPolicy
- ``break_glass``: BreakGlassGrant
- ``channel_quarantine``: Channel, Quarantine
- ``capture``: CapturedEmail, CapturedWebhook
- ``outbound_webhook``: WebhookSubscription, OutboundDelivery, Transport
- ``saved_search``: SavedSearch, SavedSearchAudit
- ``connector_coexistence``: Connector, CoexistencePolicy
- ``search``: SearchProvider, SearchSpec, SearchFilter, SearchHit, SearchResultPage
- ``participant_relationship``: Participant, CaseRelationship
- ``legal_hold``: LegalHold
- ``viewer``: Annotation, CheckoutLock, Rendition (T5 viewer pipeline)
- ``retention``: RetentionPolicy, DispositionRequest, CaseArchival,
  DisposalFreeze, OrphanCaseProposal
- ``document_search``: DocumentSearchProvider
- ``extraction``: BarcodeDecoder, OcrProvider, TemplateZoneExtractor,
  HandwritingRecognizer, ScanBatchRepository (T1 assisted indexing)
"""

from app.domain.ports.audit import AuditEventStore
from app.domain.ports.break_glass import BreakGlassGrantRepository
from app.domain.ports.capture import (
    CapturedEmailRepository,
    CapturedWebhookRepository,
)
from app.domain.ports.channel_quarantine import (
    ChannelRepository,
    QuarantineRepository,
)
from app.domain.ports.connector_coexistence import (
    CoexistencePolicyRepository,
    ConnectorRepository,
)
from app.domain.ports.core import (
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    TenantRepository,
    TenantScoped,
)
from app.domain.ports.document_search import (
    DocumentSearchHit,
    DocumentSearchProvider,
    DocumentSearchResultPage,
    DocumentSearchSpec,
)
from app.domain.ports.extraction import (
    BarcodeDecoder,
    HandwritingRecognizer,
    OcrProvider,
    ScanBatchRepository,
    TemplateZoneExtractor,
)
from app.domain.ports.infrastructure import (
    DocumentStore,
    MessageBus,
    SecretProvider,
    Telemetry,
)
from app.domain.ports.legal_hold import LegalHoldRepository
from app.domain.ports.outbound_webhook import (
    OutboundDeliveryRepository,
    OutboundDeliveryTransport,
    TransportResult,
    WebhookSubscriptionRepository,
)
from app.domain.ports.outbox_idempotency import (
    IdempotencyStore,
    OutboxRecord,
    OutboxWriter,
)
from app.domain.ports.participant_relationship import (
    CaseRelationshipRepository,
    ParticipantRepository,
)
from app.domain.ports.retention import (
    CaseArchivalRepository,
    DisposalFreezeRepository,
    DispositionRequestRepository,
    OrphanCaseProposalRepository,
    RetentionPolicyRepository,
)
from app.domain.ports.role_session import (
    RoleRepository,
    SessionPolicyRepository,
    SessionRepository,
)
from app.domain.ports.saved_search import (
    SavedSearchAuditRepository,
    SavedSearchRepository,
)
from app.domain.ports.search import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.domain.ports.sla import SlaClockRepository
from app.domain.ports.task_queue import (
    QueueRepository,
    TaskRepository,
    WorkflowInstanceRepository,
    WorkflowTransitionLog,
)
from app.domain.ports.viewer import (
    AnnotationRepository,
    CheckoutLockRepository,
    RenditionRepository,
)
from app.domain.ports.workflow import (
    WorkflowDefinitionRepository,
    WorkflowVersionRepository,
)

__all__ = [
    # Core
    "CaseRepository",
    "CaseTypeRepository",
    "CaseTypeVersionRepository",
    "DocumentRepository",
    "TenantRepository",
    "TenantScoped",
    # Workflow
    "WorkflowDefinitionRepository",
    "WorkflowVersionRepository",
    # Task/Queue/Workflow
    "QueueRepository",
    "TaskRepository",
    "WorkflowInstanceRepository",
    "WorkflowTransitionLog",
    # SLA
    "SlaClockRepository",
    # Audit
    "AuditEventStore",
    # Break-glass
    "BreakGlassGrantRepository",
    # Outbox/Idempotency
    "IdempotencyStore",
    "OutboxRecord",
    "OutboxWriter",
    # Infrastructure
    "DocumentStore",
    "MessageBus",
    "SecretProvider",
    "Telemetry",
    # Role/Session
    "RoleRepository",
    "SessionPolicyRepository",
    "SessionRepository",
    # Channel/Quarantine
    "ChannelRepository",
    "QuarantineRepository",
    # Capture
    "CapturedEmailRepository",
    "CapturedWebhookRepository",
    # Outbound Webhook
    "OutboundDeliveryRepository",
    "OutboundDeliveryTransport",
    "TransportResult",
    "WebhookSubscriptionRepository",
    # Saved Search
    "SavedSearchAuditRepository",
    "SavedSearchRepository",
    # Connector/Coexistence
    "ConnectorRepository",
    "CoexistencePolicyRepository",
    # Search
    "SearchFilter",
    "SearchHit",
    "SearchProvider",
    "SearchResultPage",
    "SearchSpec",
    # Participant/Relationship
    "CaseRelationshipRepository",
    "ParticipantRepository",
    # Legal Hold
    "LegalHoldRepository",
    # Viewer pipeline (T5 / FM-023 ext)
    "AnnotationRepository",
    "CheckoutLockRepository",
    "RenditionRepository",
    # Retention
    "CaseArchivalRepository",
    "DisposalFreezeRepository",
    "DispositionRequestRepository",
    "OrphanCaseProposalRepository",
    "RetentionPolicyRepository",
    # Document Search
    "DocumentSearchHit",
    "DocumentSearchProvider",
    "DocumentSearchResultPage",
    "DocumentSearchSpec",
    # Extraction + scan-batch inbox (T1 assisted indexing)
    "BarcodeDecoder",
    "HandwritingRecognizer",
    "OcrProvider",
    "ScanBatchRepository",
    "TemplateZoneExtractor",
]
