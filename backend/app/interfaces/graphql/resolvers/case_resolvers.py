"""Case/document query and field resolvers plus schema-wiring validation (P13-US11 / FM-057)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.cases import list_cases, view_case
from app.application.cases.redactor import redact_case_data
from app.application.documents import list_documents
from app.application.documents.list_documents import ListDocumentsForCaseQuery
from app.domain.case import Case
from app.domain.document import Document
from app.domain.errors import ValidationFailed
from app.interfaces.graphql.resolvers.context import (
    _RESOLVERS,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ResolverContext,
    _coerce_pagination_int,
    _register,
    _reject_unknown_args,
)
from app.interfaces.graphql.schema import SCHEMA_MODEL

# ---------------------------------------------------------------------------
# Query resolvers
# ---------------------------------------------------------------------------


@_register("query_case")
async def _query_case(
    _parent: Mapping[str, Any] | None,
    args: Mapping[str, Any],
    ctx: ResolverContext,
) -> Mapping[str, Any] | None:
    """Look up a single Case by id, tenant-scoped + policy-enforced.

    The repository filters by ``ctx.tenant_id``; cross-tenant ids
    resolve to ``None``. The use case raises ``Forbidden`` if the
    actor lacks ``Action.CASE_READ`` — the executor translates that
    into an entry in the errors list.
    """
    # why: the schema declares exactly one argument (``id``). Silently
    # ignoring an extra argument would let a caller believe a filter
    # ran when it did not, so unknown arguments fail closed.
    _reject_unknown_args(args, allowed={"id"}, root="case")
    case_id_raw = args.get("id")
    if not isinstance(case_id_raw, str):
        raise ValidationFailed("case id must be a UUID string", field="id")
    try:
        case_id = UUID(case_id_raw)
    except ValueError as exc:
        raise ValidationFailed("invalid case id", field="id") from exc

    case = await view_case.handle(
        view_case.ViewCaseQuery(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            actor_roles=ctx.actor_roles,
            case_id=case_id,
        ),
        case_repo=ctx.case_repo,
    )
    return await _build_case_view(case, ctx)


@_register("query_cases")
async def _query_cases(
    _parent: Mapping[str, Any] | None,
    args: Mapping[str, Any],
    ctx: ResolverContext,
) -> list[Mapping[str, Any] | None]:
    """List cases in the actor's tenant, newest first.

    ``limit`` / ``offset`` are clamped to the same bounds the REST
    API advertises (AGENTS.md §17.1). The use case raises
    ``Forbidden`` if the actor lacks ``Action.CASE_READ``.
    """
    # why: same contract as the REST list endpoint (AGENTS.md 17.1)
    # and ``app.application.cases.list_cases``: limit in [1, 200],
    # offset >= 0. The check lives here (not just in the use case)
    # so a GraphQL caller gets a ``validation_failed`` error entry
    # instead of a clamped silent window.
    _reject_unknown_args(args, allowed={"limit", "offset"}, root="cases")
    limit = _coerce_pagination_int(args.get("limit", DEFAULT_PAGE_LIMIT), name="limit")
    offset = _coerce_pagination_int(args.get("offset", 0), name="offset")
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValidationFailed(
            f"limit must be in [1, {MAX_PAGE_LIMIT}]",
            field="limit",
        )
    if offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")

    cases = await list_cases.handle(
        list_cases.ListCasesQuery(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            actor_roles=ctx.actor_roles,
            limit=limit,
            offset=offset,
        ),
        case_repo=ctx.case_repo,
    )
    out: list[Mapping[str, Any] | None] = []
    for c in cases:
        out.append(await _build_case_view(c, ctx))
    return out


# ---------------------------------------------------------------------------
# Case field resolvers
# ---------------------------------------------------------------------------


@_register("case_id")
async def _case_id(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return str(parent["id"])


@_register("case_reference")
async def _case_reference(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["reference"]


@_register("case_status")
async def _case_status(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["status"]


@_register("case_priority")
async def _case_case_priority(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["priority"]


@_register("case_title")
async def _case_title(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["title"]


@_register("case_description")
async def _case_description(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["description"]


@_register("case_sensitivity")
async def _case_sensitivity(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["sensitivity"]


@_register("case_data_fields")
async def _case_data_fields(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> list[Mapping[str, Any]]:
    """Resolve ``Case.dataFields`` with policy-driven redaction.

    The redactor runs against the case's structured ``data`` using
    the actor's ``can_see_sensitive`` decision. The schema-driven
    field policy is the contract (P11-US6); the redactor never
    downgrades a granted read.
    """
    schema = parent.get("schema")
    raw_data = parent["data"]
    can_see_sensitive = bool(parent["can_see_sensitive"])

    if schema is None:
        # No schema on the parent (the list view path uses a coarse
        # sensitivity switch). Per-key redaction requires a schema;
        # when none is present we emit every key with ``redacted``
        # derived from the case-level sensitivity flag.
        redacted_data = {} if parent.get("coarse_sensitive") is True else dict(raw_data)
    else:
        redacted_data = redact_case_data(
            data=raw_data,
            schema=schema,
            can_see_sensitive=can_see_sensitive,
        )

    fields: list[Mapping[str, Any]] = []
    for key, value in redacted_data.items():
        redacted = isinstance(value, dict) and value.get("redacted") is True
        fields.append(
            {
                "key": key,
                # The wire shape for ``value`` is a string (the
                # redactor returns either the original value or the
                # marker dict; we collapse to a single string the
                # GraphQL client can render uniformly). Numbers and
                # booleans are stringified; the literal ``[REDACTED]``
                # is the only string the client sees for a sensitive
                # field.
                "value": "[REDACTED]" if redacted else _stringify_scalar(value),
                "redacted": redacted,
            }
        )
    return fields


@_register("case_documents")
async def _case_documents(
    parent: Mapping[str, Any], _args: Mapping[str, Any], ctx: ResolverContext
) -> list[Mapping[str, Any]]:
    """Resolve ``Case.documents`` through the document application layer.

    The document use case evaluates ``Action.DOCUMENT_READ`` against
    the case scope and filters by the case-type's classification
    allowlist (P03-US4). A denied read raises ``Forbidden`` — the
    executor translates that into an entry in the errors list and
    does not build the documents sub-tree.
    """
    documents = await list_documents.handle(
        ListDocumentsForCaseQuery(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            actor_roles=ctx.actor_roles,
            case_id=parent["id"] if isinstance(parent["id"], UUID) else UUID(parent["id"]),
        ),
        case_repo=ctx.case_repo,
        case_type_version_repo=ctx.case_type_version_repo,
        document_repo=ctx.document_repo,
    )
    return [_build_document_view(d) for d in documents]


# ---------------------------------------------------------------------------
# DataField resolvers
# ---------------------------------------------------------------------------


@_register("data_field_key")
async def _data_field_key(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["key"]


@_register("data_field_value")
async def _data_field_value(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["value"]


@_register("data_field_redacted")
async def _data_field_redacted(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> bool:
    return parent["redacted"]


# ---------------------------------------------------------------------------
# Document field resolvers
# ---------------------------------------------------------------------------


@_register("document_id")
async def _document_id(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return str(parent["id"])


@_register("document_file_name")
async def _document_file_name(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["file_name"]


@_register("document_classification")
async def _document_classification(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["classification"]


@_register("document_sensitivity")
async def _document_sensitivity(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["sensitivity"]


@_register("document_locked")
async def _document_locked(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> bool:
    return parent["locked"]


@_register("document_current_version")
async def _document_current_version(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> int:
    return parent["current_version"]


@_register("document_uploaded_at")
async def _document_uploaded_at(
    parent: Mapping[str, Any], _args: Mapping[str, Any], _ctx: ResolverContext
) -> str:
    return parent["uploaded_at"]


# ---------------------------------------------------------------------------
# Helper: build the ``Case`` resolver view (the dict resolvers receive)
# ---------------------------------------------------------------------------


async def _build_case_view(case: Case, ctx: ResolverContext) -> dict[str, Any]:
    """Project a domain ``Case`` into the resolver view dict.

    The dict carries every field the schema exposes; resolvers read
    from it via the parent argument. The dict also carries the
    ``schema`` (resolved once per request) and the policy-driven
    ``can_see_sensitive`` decision so the ``dataFields`` resolver
    can apply per-field redaction deterministically.
    """
    # Evaluate ``Action.CASE_READ_SENSITIVE`` once per case; the
    # helper swallows the deny by returning ``False`` so a failed
    # sensitive-read is not a 403.
    can_see_sensitive = await view_case.actor_can_see_sensitive(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        actor_roles=ctx.actor_roles,
        case_id=case.id,
    )

    # Resolve the case-type version so the dataFields resolver can
    # use the schema's sensitive_field_keys(). Missing version ->
    # schema=None; the dataFields resolver then falls back to the
    # coarse sensitivity switch. The redactor never raises on a
    # missing schema.
    version = await ctx.case_type_version_repo.get(ctx.tenant_id, case.case_type_version_id)
    schema = version.schema if version is not None else None

    return {
        "id": case.id,
        "reference": case.reference.value,
        "status": case.status.value,
        "priority": case.priority,
        "title": case.title,
        "description": case.description,
        "sensitivity": case.sensitivity,
        "data": case.data,
        "schema": schema,
        "can_see_sensitive": can_see_sensitive,
        "coarse_sensitive": case.sensitivity in {"restricted", "phi"},
    }


def _build_document_view(document: Document) -> dict[str, Any]:
    """Project a domain ``Document`` into the resolver view dict."""
    return {
        "id": document.id,
        "file_name": document.file_name,
        "classification": document.classification,
        "sensitivity": document.sensitivity.value
        if hasattr(document.sensitivity, "value")
        else str(document.sensitivity),
        "locked": document.locked,
        "current_version": document.current_version,
        "uploaded_at": document.created_at.isoformat(),
    }


def _stringify_scalar(value: Any) -> str:
    """Best-effort scalar coercion for the GraphQL ``value`` field.

    The Case ``data`` payload is a dict of JSON-decoded values; the
    GraphQL schema declares ``value: String!`` for the v1 surface.
    Numbers, booleans, and ISO-8601 timestamps are stringified. The
    redactor never passes a dict into this path (its output is
    already a dict whose ``redacted: true`` flag is caught upstream).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    # Dicts and complex values fall through to JSON; this is the
    # safe-by-default path for fields that happen to be JSON-shaped
    # (e.g. an ``enum_values`` declaration). It never replaces the
    # redaction marker, which is caught upstream.
    return json.dumps(value, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# Validation: every declared field has a registered resolver
# ---------------------------------------------------------------------------


def validate_schema_wiring() -> None:
    """Raise if a schema-declared field has no resolver.

    Called by the executor at startup so a missing resolver is
    caught early. The validation lives here (not in ``schema.py``)
    because the resolver map is built by the registration
    decorators above.
    """
    declared: set[str] = set()
    for obj in SCHEMA_MODEL.values():
        for fld in obj.fields:
            declared.add(fld.resolver)
    missing = declared - set(_RESOLVERS)
    if missing:
        raise RuntimeError(f"GraphQL resolvers missing for fields: {sorted(missing)}")
