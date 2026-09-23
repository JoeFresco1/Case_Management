"""Protocol seams and subject/event constants for intake submission.

The intake use case talks to two application-level ports that are not
domain entities — a single-form-version loader and the latest-published
binding lookup — plus the two optional capability seams that make
lookup pre-population and external signature verification pluggable.
Keeping them together documents the integration surface in one place.

# INTEGRATION_SEAM: LookupProvider / SignatureConnector

- ``LookupProvider`` -> local configured-source adapter today; a future
  enterprise directory/registry adapter implements the same Protocol.
- ``SignatureConnector`` -> P14 e-signature connector placeholder (no
  vendor SDK in domain/application per spec T3). ``None`` means "no
  external signer wired" and fails closed (see
  :func:`.field_processing.check_signature_fields`).
- ``DocumentStore`` and the repositories are domain ports declared in
  :mod:`app.domain.ports`; ``handle`` binds them at the call site.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from app.domain.form import FormBinding, FormVersion

# why literals: audit/outbox subject types and the DocumentAdded event
# key are part of the recorded contract; they must not drift with a
# rename of this package.
SUBJECT_TYPE_CASE = "case"
SUBJECT_TYPE_DOCUMENT = "document"
DOCUMENT_ADDED = "DocumentAdded"


class FormVersionLoader(Protocol):
    """Single-form-version lookup seam."""

    async def get_by_id(self, tenant_id: UUID, form_version_id: UUID) -> FormVersion | None: ...


class FormBindingRepository(Protocol):
    """Latest-published binding lookup seam (unpublished bindings invisible)."""

    async def get_published_for_key(self, tenant_id: UUID, form_key: str) -> FormBinding | None: ...


class LookupProvider(Protocol):
    """Deterministic lookup-source seam for ``lookup`` field pre-population.

    The local adapter resolves from a configured in-tenant source (static
    value sets in tests); an enterprise directory adapter implements the
    same shape in production. Returning ``None`` means "no value for this
    query" (the field stays blank and required-validation decides). Raising
    ``DependencyUnavailable`` signals a down source and rolls the intake
    back; raising ``ValidationFailed`` names a misconfigured source.
    """

    async def resolve(
        self, tenant_id: UUID, source: str, query: Mapping[str, Any]
    ) -> str | None: ...


class SignatureConnector(Protocol):
    """External e-signature verification seam (P14 connector placeholder).

    ``handwritten`` / ``typed`` signatures never reach this port (they are
    validated as non-blank strings). ``external`` signatures delegate here:
    ``True`` accepts, ``False`` rejects with ``ValidationFailed`` naming the
    field. ``DependencyUnavailable`` propagates so the intake rolls back.
    """

    async def verify_external(
        self, tenant_id: UUID, connector: str, payload: Mapping[str, Any]
    ) -> bool: ...


class _ClockLike(Protocol):
    """Minimal clock protocol (avoids importing FastAPI-bound ``app.deps``)."""

    def now(self) -> Any: ...


__all__ = [
    "DOCUMENT_ADDED",
    "FormBindingRepository",
    "FormVersionLoader",
    "LookupProvider",
    "SignatureConnector",
    "SUBJECT_TYPE_CASE",
    "SUBJECT_TYPE_DOCUMENT",
    "_ClockLike",
]
