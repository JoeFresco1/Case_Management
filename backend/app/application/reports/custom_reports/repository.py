"""Persistence port for report definitions — kept here so the application layer is testable.

The port is duck-typed so a Protocol class is unnecessary; the
application layer calls ``add`` / ``get`` / ``list`` / ``update``
against any object that exposes them. The test layer supplies an
in-memory implementation; the HTTP router instantiates the
process-scoped singleton declared below. Split out of the former
single-file ``custom_reports.py`` during the R2 package split.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.errors import NotFound, ValidationFailed

from .shapes import ReportDefinition


class ReportDefinitionRepository:
    """Persistence port for :class:`~.shapes.ReportDefinition` (P12-US7 / FM-053).

    The port is duck-typed so a Protocol class is unnecessary; the
    application layer calls ``add`` / ``get`` / ``list`` / ``update``
    against any object that exposes them. The test layer supplies an
    in-memory implementation; the HTTP router instantiates the
    process-scoped singleton declared below.
    """

    def __init__(self) -> None:
        self._by_id: dict[tuple[UUID, UUID], ReportDefinition] = {}
        self._by_key: dict[tuple[UUID, str], ReportDefinition] = {}

    async def add(self, definition: ReportDefinition) -> None:
        """Store a new definition, rejecting a duplicate key within the tenant."""
        key = (definition.tenant_id, definition.key)
        if key in self._by_key:
            raise ValidationFailed(
                f"report definition with key {definition.key!r} already exists",
                field="key",
            )
        self._by_id[(definition.tenant_id, definition.id)] = definition
        self._by_key[key] = definition

    async def get(self, tenant_id: UUID, definition_id: UUID) -> ReportDefinition | None:
        """Return the definition when it exists in ``tenant_id``, else ``None``."""
        return self._by_id.get((tenant_id, definition_id))

    async def get_by_key(self, tenant_id: UUID, key: str) -> ReportDefinition | None:
        """Return the definition by its author-visible key, else ``None``."""
        return self._by_key.get((tenant_id, key))

    async def list(self, tenant_id: UUID) -> list[ReportDefinition]:
        """Return every definition belonging to ``tenant_id``."""
        return [d for (tid, _), d in self._by_id.items() if tid == tenant_id]

    async def update(self, definition: ReportDefinition) -> None:
        """Replace an existing definition, keeping the by-key index coherent.

        Raises:
            NotFound: if the definition id is absent from the tenant
                (cross-tenant ids never confirm existence — Constitution V).
        """
        prior = self._by_id.get((definition.tenant_id, definition.id))
        if prior is None:
            raise NotFound(f"report definition {definition.id} not found in tenant")
        self._by_id[(definition.tenant_id, definition.id)] = definition
        # The key is stable across updates by convention; if a caller
        # rotates the key, the by-key index must follow so a future
        # ``get_by_key`` resolves to the updated row.
        if prior.key != definition.key:
            self._by_key.pop((definition.tenant_id, prior.key), None)
        self._by_key[(definition.tenant_id, definition.key)] = definition


# Process-wide singleton. The router wires this in; the test layer
# replaces it via the function-level repository parameter so the
# use cases remain hermetic.
_default_repository: ReportDefinitionRepository | None = None


def default_repository() -> ReportDefinitionRepository:
    """Return (lazily creating) the process-wide definition repository."""
    global _default_repository  # noqa: PLW0603
    if _default_repository is None:
        _default_repository = ReportDefinitionRepository()
    return _default_repository


def reset_default_repository() -> None:
    """Clear the process-wide singleton. Tests use this to hermetic-seal."""
    global _default_repository  # noqa: PLW0603
    _default_repository = None


__all__ = [
    "ReportDefinitionRepository",
    "default_repository",
    "reset_default_repository",
    "_default_repository",
]
