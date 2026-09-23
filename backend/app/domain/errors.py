"""Domain errors.

Domain raises these. The HTTP layer maps them to structured error responses.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base for all domain errors. Never includes sensitive values."""


class InvariantViolation(DomainError):  # noqa: N818 - stable public domain contract
    """An attempt to construct or transition an entity in an invalid way."""


class NotFound(DomainError):  # noqa: N818 - stable public domain contract
    """The referenced entity does not exist (or is not visible in this tenant)."""


class ValidationFailed(DomainError):  # noqa: N818 - stable public domain contract
    """Required field missing or value does not match schema."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class Conflict(DomainError):  # noqa: N818 - stable public domain contract
    """Optimistic concurrency or uniqueness conflict."""


class Forbidden(DomainError):  # noqa: N818 - stable public domain contract
    """The actor is not permitted to perform this action.

    Carries an optional safe ``details`` mapping (never PHI/SQL) so the
    HTTP boundary can render a structured ``forbidden`` envelope with a
    machine-readable ``reason`` (e.g. ``separation_of_duties_violation``).
    """

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = dict(details) if details else {}


class UnsupportedTransition(DomainError):  # noqa: N818 - stable public domain contract
    """The transition is not allowed from the current state."""


class DependencyUnavailable(DomainError):  # noqa: N818 - stable public domain contract
    """A required adapter (storage, bus, scan, etc.) is not reachable."""


class DocumentLocked(DomainError):  # noqa: N818 - stable public domain contract
    """The document is locked and refuses the requested mutation.

    Maps to HTTP 423 Locked (RFC 4918 §11.3). The original document
    is preserved; the error is the contract that the operation was
    refused without side effects.
    """
