"""SQL composition provider for the admin governance HTTP boundary.

This adapter binds the local publication repository, governance policy, and
role repository to the infra-free application contract. The provider keeps
all request ports on the same SQLAlchemy session where persistence is
transactional; an Azure adapter can implement the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.configuration.http_dependencies import (
    AdminGovernanceHttpRepositories,
)
from app.infrastructure.governance.local_governance_policy import LocalGovernancePolicy
from app.infrastructure.persistence.repositories import (
    SqlConfigurationPublicationRepository,
    SqlRoleRepository,
)


class SqlAdminGovernanceHttpDependencyProvider:
    """Bind the admin governance HTTP ports to one SQLAlchemy session."""

    def __init__(self) -> None:
        # why: policy overrides are application-lifetime configuration, not
        # request-local state, so each request observes the same policy seam.
        self._policy = LocalGovernancePolicy()

    def for_session(self, session: object) -> AdminGovernanceHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminGovernanceHttpRepositories(
            publication=SqlConfigurationPublicationRepository(db),
            policy=self._policy,
            role=SqlRoleRepository(db),
        )


__all__ = ["SqlAdminGovernanceHttpDependencyProvider"]
