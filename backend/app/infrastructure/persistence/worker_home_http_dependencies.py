"""SQL composition provider for the worker-home HTTP boundary (T5 worker UX).

Dashboard/preference/policy rows are process-local singletons (synthetic
tenants); audit + outbox stay SQL so every mutation remains audit-atomic
in the request transaction (FR-L02). An Azure adapter can implement the
same application contract later without touching the router.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.worker_home.http_dependencies import WorkerHomeHttpRepositories
from app.application.worker_home.worker_home import (
    DashboardStore,
    PreferencePolicyStore,
    PreferenceStore,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore, SqlOutboxWriter


class SqlWorkerHomeHttpDependencyProvider:
    """Bind worker-home HTTP ports to one SQLAlchemy session."""

    def __init__(
        self,
        *,
        dashboards: DashboardStore,
        preferences: PreferenceStore,
        policies: PreferencePolicyStore,
    ) -> None:
        self._dashboards = dashboards
        self._preferences = preferences
        self._policies = policies

    def for_session(self, session: object) -> WorkerHomeHttpRepositories:
        """Create adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return WorkerHomeHttpRepositories(
            dashboards=self._dashboards,
            preferences=self._preferences,
            policies=self._policies,
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlWorkerHomeHttpDependencyProvider"]
