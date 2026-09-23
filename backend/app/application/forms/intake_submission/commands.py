"""Intake submission commands, result, and the duck-typed actor proxy.

The frozen dataclasses that cross the use-case boundary: what the HTTP
layer packs into :class:`SubmitIntakeCommand`, what
:func:`~.use_case.handle` returns in :class:`SubmitIntakeResult`, and
:class:`_ActorProxy` — the lightweight stand-in for ``UserContext``
that keeps FastAPI out of the application layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.case import Case


@dataclass(frozen=True, slots=True)
class AttachmentBlob:
    """Inline file bytes for one ``attachment`` form field on intake.

    On the in-case resubmit path attachments are uploaded first via
    ``upload_document`` and referenced by ``{"document_id": ...}``. On
    intake there is no case row yet, so the bytes travel with the
    submission and are stored in the same transaction as the new case.
    """

    file_name: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SubmitIntakeCommand:
    """Input to :func:`~.use_case.handle`.

    ``submitted_data`` holds every worker-typed value keyed by field key
    (including attachment references of shape ``{"document_id": ...}``
    when the document was pre-uploaded). ``attachments`` holds inline
    blobs for attachment fields whose bytes travel with the form. ``title``
    falls back to the ``title`` field value when omitted so the form is
    the single source of truth for the case title.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    form_key: str
    submitted_data: Mapping[str, Any]
    attachments: Mapping[str, AttachmentBlob]
    title: str = ""
    description: str = ""
    priority: str = "normal"
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitIntakeResult:
    """Post-commit outcome: the new case plus routing metadata for the UI."""

    case: Case
    hidden_keys: frozenset[str]
    calculated_keys: frozenset[str]
    document_ids: Mapping[str, UUID]


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (avoids FastAPI import)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "AttachmentBlob",
    "SubmitIntakeCommand",
    "SubmitIntakeResult",
    "_ActorProxy",
]
