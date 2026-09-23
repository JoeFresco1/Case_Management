"""Pydantic schemas for the extension registry HTTP surface.

Wire projections of ``app.domain.extension.ExtensionSpec`` and friends.
The schemas do not invent fields; the OpenAPI types match the
declarative contract so ``openapi-typescript`` regenerates without a
drift check failing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PolicyActionIn(BaseModel):
    """Body shape for a policy action contribution.

    Mirrors ``app.domain.extension.PolicyActionSpec``; ``is_sensitive``
    defaults to False so a safe-by-default request body is also a
    valid one.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        min_length=5,
        max_length=80,
        description="Wire identifier, e.g. 'ext.my-module.create'",
    )
    description: str = Field(min_length=1, max_length=1024)
    is_sensitive: bool = False


class EventTypeIn(BaseModel):
    """Body shape for a single event type contribution.

    ``payload_keys`` is the whitelist; anything not in the list is
    rejected at publish time (``domain.events.validate_payload``). The
    redaction list in ``domain.extension`` rejects PHI-named keys at
    registration time so a module with PHI in its schema is blocked
    before it ships.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(
        min_length=3,
        max_length=128,
        description="PascalCase dotted name, e.g. 'TenantExt.FooBar'",
    )
    version: int = Field(ge=1, le=1_000_000)
    description: str = Field(min_length=1, max_length=1024)
    payload_keys: list[str] = Field(default_factory=list, max_length=64)


class RegisterExtensionRequest(BaseModel):
    """Body for ``POST /api/v1/extensions``."""

    model_config = ConfigDict(extra="forbid")

    module_id: str = Field(
        min_length=5,
        max_length=40,
        description="Stable identifier, e.g. 'ext.my-module'",
    )
    version: int = Field(ge=1, le=1_000_000)
    description: str = Field(min_length=1, max_length=1024)
    author: str = Field(default="", max_length=256)
    policy_actions: list[PolicyActionIn] = Field(default_factory=list, max_length=64)
    event_types: list[EventTypeIn] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyActionOut(BaseModel):
    action: str
    description: str
    is_sensitive: bool


class EventTypeOut(BaseModel):
    event_type: str
    version: int
    description: str
    payload_keys: list[str]


class ExtensionOut(BaseModel):
    """Catalog entry — the response shape of ``POST /extensions`` and
    ``GET /extensions``."""

    module_id: str
    version: int
    description: str
    author: str
    policy_actions: list[PolicyActionOut]
    event_types: list[EventTypeOut]
    installed_at: UUID
    audit_event_id: UUID


class ExtensionsCatalog(BaseModel):
    """Response shape for ``GET /api/v1/extensions/catalog``.

    The catalog is intentionally flat (not ``Page[T]``); the registry
    is bounded by the number of modules a tenant can install. The
    ``source`` field documents that the entries come from the
    process-local registry; FM-047 slice 2 will overlay the persistent
    table on top of this.
    """

    items: list[ExtensionOut]
    source: str = "ExtensionRegistry"


class ExtensionEventCatalog(BaseModel):
    """Response shape for ``GET /api/v1/extensions/events/catalog``.

    Composes the built-in catalog (``app.domain.events.EVENT_CATALOG``)
    with every event type the extensions have registered. Built-in
    entries take precedence on duplicate keys; ``source`` is the union
    shape documented in ``iter_event_specs``.
    """

    items: list[EventTypeOut]
    source: str = "EVENT_CATALOG+ExtensionRegistry"
