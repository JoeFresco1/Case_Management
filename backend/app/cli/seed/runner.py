"""Idempotent seed runner: tenant, users, case type, workflow, demo case.

Writes the full first-slice vertical through the application layer's
publish/start use cases (never bypassing them) inside one
``session_scope`` transaction, in the order the demo dataset requires:
tenant → users → published ServiceRequest case type → published
workflow blueprint → demo case with its initial review task. Every
lookup short-circuits on an existing row so re-runs are safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.application.configuration import publish_case_type as publish_case_type_uc
from app.application.configuration.validate_definition import validate_definition
from app.application.workflow import publish_workflow as publish_workflow_uc
from app.application.workflow import start_workflow as start_workflow_uc
from app.config import get_settings
from app.db import session_scope
from app.deps import UserContext, register_local_user
from app.domain.case import Case
from app.domain.case_type import CaseType, CaseTypeVersion, CaseTypeVersionStatus
from app.domain.workflow import WorkflowDefinition, WorkflowVersion
from app.infrastructure.persistence import models  # noqa: F401 — register models
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore, SqlOutboxWriter
from app.infrastructure.persistence.repositories import (
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlQueueRepository,
    SqlWorkflowDefinitionRepository,
    SqlWorkflowVersionRepository,
)
from app.infrastructure.persistence.repositories.case_repository import SqlCaseRepository
from app.infrastructure.persistence.repositories.sla_clock_repository import SqlSlaClockRepository
from app.infrastructure.persistence.repositories.task_repository import SqlTaskRepository
from app.infrastructure.persistence.repositories.workflow_instance_repository import (
    SqlWorkflowInstanceRepository,
)

from .blueprint import _service_request_workflow_blueprint
from .dataset import (
    TENANT_KEY,
    TENANT_NAME,
    _service_request_schema,
    _synthetic_users,
)

__all__ = ["_seed"]


async def _seed() -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """Idempotent seed.

    Returns a summary dict suitable for ``typer.echo``. Re-runs are
    safe: every ``get_by_key`` / ``get_by_id`` lookup short-circuits
    and the existing row is left untouched.
    """
    settings = get_settings()
    if not settings.synthetic_environment:
        # Why a hard block: the seed writes obviously fake identities;
        # running it against a real prod database is the worst-case
        # outcome for an operator who confused ``APP_ENV=prod`` with
        # ``APP_ENV=local``. We fail closed instead.
        raise RuntimeError(
            "seed refuses to run when SYNTHETIC_ENVIRONMENT is False "
            "(AGENTS.md §18 — synthetic data must never touch prod)"
        )

    summary: dict[str, Any] = {
        "tenant": None,
        "users": [],
        "case_type": None,
        "workflow": None,
    }

    # Append-only audit guards (T039): installed here because the seed
    # writes audit rows through the ORM and ``app.db`` no longer installs
    # them (layer contract). Same pair the API composition root installs.
    from app.db import get_engine  # noqa: PLC0415  (composition-root wiring)
    from app.infrastructure.persistence.audit_events import (  # noqa: PLC0415
        install_audit_immutability_execute_listener,
        install_audit_immutability_listener,
    )

    install_audit_immutability_listener()
    install_audit_immutability_execute_listener(get_engine())

    async with session_scope() as session:
        # --- Tenant ---------------------------------------------------------
        result = await session.execute(select(models.Tenant).where(models.Tenant.key == TENANT_KEY))
        tenant_row = result.scalar_one_or_none()
        if tenant_row is None:
            tenant_row = models.Tenant(
                id=uuid4(),
                key=TENANT_KEY,
                name=TENANT_NAME,
                created_at=datetime.now(tz=UTC),
            )
            session.add(tenant_row)
            await session.flush()
        tenant_id = tenant_row.id
        summary["tenant"] = {"id": str(tenant_id), "key": TENANT_KEY}

        # --- Users ----------------------------------------------------------
        for su in _synthetic_users(tenant_id):
            user_row = (
                await session.execute(
                    select(models.User).where(
                        models.User.tenant_id == tenant_id,
                        models.User.key == su.key,
                    )
                )
            ).scalar_one_or_none()
            if user_row is None:
                now = datetime.now(tz=UTC)
                session.add(
                    models.User(
                        id=su.user_id,
                        tenant_id=tenant_id,
                        key=su.key,
                        display_name=su.display_name,
                        roles=list(su.roles),
                        is_system=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
            # Register with the dev-header IdP so X-Dev-User=intake
            # resolves even when the DB session is restarted. We use
            # the deterministic UUID — NOT the row's id — so callers
            # in tests can hard-code ``X-Dev-User=intake``.
            register_local_user(
                UserContext(
                    user_id=su.user_id,
                    tenant_id=tenant_id,
                    display_name=su.display_name,
                    roles=su.roles,
                )
            )
            summary["users"].append(
                {
                    "key": su.key,
                    "user_id": str(su.user_id),
                    "email": su.email,
                }
            )

        # --- CaseType + published version ----------------------------------
        case_type_repo = SqlCaseTypeRepository(session)
        case_type_version_repo = SqlCaseTypeVersionRepository(session)

        case_type = await case_type_repo.get_by_key(tenant_id, "ServiceRequest")
        if case_type is None:
            case_type = CaseType.create(
                tenant_id=tenant_id,
                key="ServiceRequest",
                name="Service Request",
                description="Synthetic case type for the local demo dataset.",
                sensitivity_default="internal",
                now=datetime.now(tz=UTC),
            )
            await case_type_repo.add(case_type)

        # Always make sure a published version exists (idempotent: if
        # a draft exists, publish it; if a published one exists, do
        # nothing).
        published = await case_type_version_repo.get_by_key(tenant_id, "ServiceRequest")
        if published is None:
            draft = CaseTypeVersion.draft(
                tenant_id=tenant_id,
                case_type_id=case_type.id,
                version_number=1,
                schema=_service_request_schema(),
                now=datetime.now(tz=UTC),
            )
            await case_type_version_repo.add(draft)
            await session.flush()

            audit = SqlAuditEventStore(session)

            class _Clock:
                def now(self) -> datetime:
                    return datetime.now(tz=UTC)

            await publish_case_type_uc.handle(
                publish_case_type_uc.PublishCaseTypeVersionCommand(
                    tenant_id=tenant_id,
                    actor_id=next(
                        u.user_id for u in _synthetic_users(tenant_id) if u.key == "supervisor"
                    ),
                    case_type_version_id=draft.id,
                    correlation_id="seed-cli",
                ),
                case_type_repo=case_type_repo,
                case_type_version_repo=case_type_version_repo,  # type: ignore[arg-type]
                audit_store=audit,
                clock=_Clock(),
            )
            # Why flush here: the session factory sets ``autoflush=False``,
            # so the status flip + current-version pointer staged by the
            # publish use case are invisible to subsequent SELECTs until
            # flushed. Without this, the re-read below queries the
            # pre-publish row state and reports "no published version".
            await session.flush()
            # Re-read through the published-version query. The publish use
            # case stages the lifecycle update in this session; retaining the
            # draft object here would bind the workflow/demo case to a draft.
            published = await case_type_version_repo.get_by_key(tenant_id, "ServiceRequest")
            if published is None:
                raise RuntimeError("seed failed to resolve the published ServiceRequest version")
            summary["case_type"] = {
                "key": "ServiceRequest",
                "version": 1,
                "status": CaseTypeVersionStatus.PUBLISHED.value,
            }
        else:
            summary["case_type"] = {
                "key": "ServiceRequest",
                "version": published.version_number,
                "status": published.status.value,
            }

        # --- Workflow blueprint (P03-US5, FM-024) ----------------------------
        # Build a draft workflow version in memory and run the
        # validator. The blueprint is the exact shape the case-creation
        # seed will eventually persist once the SQL workflow repos
        # land; validating it now means a malformed guard (missing
        # classification, bad operator, automatic cycle) fails the
        # seed rather than passing through to a workflow that traps a
        # case at runtime. The validator's known_classifications
        # cross-check uses the case-type's own allowlist.
        known_classifications = (
            published.schema.document_classifications
            if published is not None
            else ("Contract", "Correspondence", "Evidence")
        )
        workflow_blueprint = _service_request_workflow_blueprint(
            tenant_id=tenant_id,
            known_classifications=known_classifications,
        )
        workflow_repo = SqlWorkflowDefinitionRepository(session)
        workflow_version_repo = SqlWorkflowVersionRepository(session)
        queue_repo = SqlQueueRepository(session)
        for key, name in (("review_queue", "Review Queue"), ("intake_queue", "Intake Queue")):
            queue = (
                await session.execute(
                    select(models.Queue).where(
                        models.Queue.tenant_id == tenant_id, models.Queue.key == key
                    )
                )
            ).scalar_one_or_none()
            if queue is None:
                session.add(
                    models.Queue(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        key=key,
                        name=name,
                        created_at=datetime.now(tz=UTC),
                        updated_at=datetime.now(tz=UTC),
                    )
                )
        await session.flush()
        workflow_definition = await workflow_repo.get_by_key(tenant_id, "ServiceRequestWorkflow")
        if workflow_definition is None:
            workflow_definition = WorkflowDefinition.create(
                tenant_id=tenant_id,
                key="ServiceRequestWorkflow",
                name="Service Request Workflow",
                now=datetime.now(tz=UTC),
            )
            await workflow_repo.add(workflow_definition)
            await session.flush()
        workflow_version = WorkflowVersion.draft(
            tenant_id=tenant_id,
            workflow_definition_id=workflow_definition.id,
            version_number=1,
            states=workflow_blueprint["states"],
            transitions=workflow_blueprint["transitions"],
            now=datetime.now(tz=UTC),
        )
        validate_definition(
            workflow_version,
            known_queues=await queue_repo.list_keys(tenant_id),
            known_classifications=known_classifications,
        )
        existing_version = await workflow_version_repo.get_by_version_number(
            tenant_id, workflow_definition.id, 1
        )
        if existing_version is None:
            await workflow_version_repo.add(workflow_version)
            await session.flush()
            await publish_workflow_uc.handle(
                publish_workflow_uc.PublishWorkflowVersionCommand(
                    tenant_id=tenant_id,
                    actor_id=next(
                        u.user_id for u in _synthetic_users(tenant_id) if u.key == "supervisor"
                    ),
                    workflow_version_id=workflow_version.id,
                    correlation_id="seed-cli",
                ),
                workflow_repo=workflow_repo,
                workflow_version_repo=workflow_version_repo,
                queue_repo=queue_repo,
                audit_store=SqlAuditEventStore(session),
                clock=_Clock(),
            )
            # Same autoflush=False reason as the case-type publish above:
            # ``start_workflow`` resolves the published version via SELECT,
            # so the workflow publish must be flushed first.
            await session.flush()
        else:
            workflow_version = existing_version
        summary["workflow"] = {
            "key": "ServiceRequestWorkflow",
            "version": 1,
            "transition_keys": list(workflow_version.transition_keys()),
            "guarded_transitions": [
                t.key for t in workflow_version.transitions if t.guard is not None
            ],
        }

        # --- Synthetic demo case + initial review task ---------------------
        case_repo = SqlCaseRepository(session)
        existing_case = (
            await session.execute(
                select(models.Case).where(
                    models.Case.tenant_id == tenant_id,
                    models.Case.title == "Synthetic Service Request Demo",
                )
            )
        ).scalar_one_or_none()
        if existing_case is None:
            now = datetime.now(tz=UTC)
            case = Case.create(
                tenant_id=tenant_id,
                reference=await case_repo.next_reference(
                    tenant_id, now=now, type_key=case_type.key
                ),
                case_type_version=published,
                title="Synthetic Service Request Demo",
                description="Synthetic demo case.",
                data={"title": "Synthetic Service Request Demo", "priority": "normal"},
                priority="normal",
                created_by=next(
                    u.user_id for u in _synthetic_users(tenant_id) if u.key == "intake"
                ),
                now=now,
            )
            # T018: the demo case opens in the workflow's initial state.
            # First-assignment keeps `version_token=1` (not a mutation).
            case = case.set_workflow_state(state="intake", now=now)
            await case_repo.add(case)
            starter = start_workflow_uc.StartWorkflowCommand(
                tenant_id=tenant_id,
                actor_id=case.created_by,
                case_id=case.id,
                workflow_key="ServiceRequestWorkflow",
                correlation_id="seed-cli",
            )
            started = await start_workflow_uc.handle(
                starter,
                workflow_repo=workflow_repo,
                workflow_version_repo=workflow_version_repo,
                instance_repo=SqlWorkflowInstanceRepository(session),
                task_repo=SqlTaskRepository(session),
                audit=SqlAuditEventStore(session),
                outbox=SqlOutboxWriter(session),
                clock=_Clock(),
                # The blueprint's `start_sla` action stages the 48h
                # `review_due` clock in the same transaction (T077).
                sla_clock_repo=SqlSlaClockRepository(session),
            )
            if not started.tasks:
                # Safety net for workflows without `create_task` welcome
                # actions; the ServiceRequest blueprint always produces
                # the review task above, so this stays dormant.
                task = models.Task(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=case.id,
                    type="review",
                    title="Review service request",
                    status="open",
                    assignee_queue_id="review_queue",
                    priority="normal",
                    version_token=1,
                    created_at=now,
                    updated_at=now,
                    created_by_transition="initial",
                )
                session.add(task)
            summary["demo_case"] = str(case.id)

    return summary
