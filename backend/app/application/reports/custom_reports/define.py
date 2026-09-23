"""Use case: define a custom report (P12-US7 / FM-053).

Validates the closed-vocabulary spec, evaluates the authorization
policy, then persists a server-stamped :class:`~.shapes.ReportDefinition`.
Split out of the former single-file ``custom_reports.py`` during the R2
package split.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.authorization import Action, Resource, authorize

from .helpers import _ActorProxy
from .repository import ReportDefinitionRepository
from .shapes import DefineCustomReportCommand, ReportDefinition
from .validation import _validate_command


async def define_custom_report(
    cmd: DefineCustomReportCommand,
    *,
    repo: ReportDefinitionRepository,
    clock: Any,
) -> ReportDefinition:
    """Validate, persist, and return a new :class:`~.shapes.ReportDefinition`.

    Authorization: ``ADMIN_TENANT_CONFIG``. The repository is
    in-memory; a future SQL adapter lands behind the same port.

    Validation runs first so a malformed spec never reaches the
    repository. The created-by + created-at fields are stamped by
    the use case (never the caller) so the audit chain always agrees
    with the row.
    """
    _validate_command(cmd)

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="report_definition"),
    )

    definition = ReportDefinition(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        key=cmd.key.strip(),
        name=cmd.name.strip(),
        description=cmd.description.strip(),
        group_by=cmd.group_by,
        aggregations=cmd.aggregations,
        filters=cmd.filters,
        time_window_hours=cmd.time_window_hours,
        created_by=cmd.actor_id,
        created_at=clock.now(),
    )
    await repo.add(definition)
    return definition


__all__ = ["define_custom_report"]
