"""Execution engine for governed queries: dispatch, recursion, and audit (P13-US11)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.audit import GRAPHQL_QUERY_EXECUTED
from app.domain.errors import (
    Forbidden,
    NotFound,
    ValidationFailed,
)
from app.interfaces.graphql.resolvers.case_resolvers import validate_schema_wiring
from app.interfaces.graphql.resolvers.context import (
    _RESOLVERS,
    GraphQLResult,
    ResolverContext,
    _record_partial_error,
)
from app.interfaces.graphql.resolvers.parsing import (
    _ParsedQuery,
    build_query_shape_audit,
    parse_query,
)
from app.interfaces.graphql.schema import (
    QUERY_TYPE,
    SCHEMA_MODEL,
    GraphQLField,
    GraphQLObjectType,
    GraphQLType,
    GraphQLTypeKind,
)


async def _resolve_selection(
    *,
    selection: dict[str, Any],
    parent: Mapping[str, Any] | None,
    obj_type: GraphQLObjectType,
    ctx: ResolverContext,
) -> Any:
    """Resolve one selection: look up the field, call the resolver, recurse."""
    field_name = selection["name"]
    field = _lookup_field(obj_type, field_name)
    resolver_fn = _RESOLVERS.get(field.resolver)
    if resolver_fn is None:
        raise RuntimeError(f"no resolver registered for field {obj_type.name}.{field_name}")
    try:
        value = await resolver_fn(parent, selection["args"], ctx)
    except Forbidden as exc:
        # The field resolver raised ``Forbidden``. Surface the denial
        # as an error entry and return a sentinel ``None`` so the
        # parent shape still includes the field path with a null
        # value. The client cannot infer whether the resource exists.
        return _PartialError(
            code="forbidden",
            message=str(exc),
        )
    except NotFound as exc:
        return _PartialError(
            code="not_found",
            message=str(exc),
        )
    # Recurse into sub-selections only if the value is a mapping
    # (object) or a list of mappings. Scalars and lists of scalars
    # have no sub-selections by definition.
    if not selection["subfields"]:
        return value
    # Strip NON_NULL but keep LIST so we can branch on LIST vs
    # OBJECT correctly.
    inner = _strip_non_null(field.type)
    if inner.kind is GraphQLTypeKind.OBJECT and isinstance(value, Mapping):
        sub_obj = SCHEMA_MODEL[inner.object_name]
        return await _resolve_object(value, selection["subfields"], sub_obj, ctx)
    if inner.kind is GraphQLTypeKind.LIST and isinstance(value, list):
        element_type = _strip_non_null(inner.inner)
        if element_type.kind is not GraphQLTypeKind.OBJECT:
            raise ValidationFailed(
                f"field {obj_type.name}.{field_name} list elements are not objects",
                field="query",
            )
        sub_obj = SCHEMA_MODEL[element_type.object_name]
        out: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                out.append(await _resolve_object(item, selection["subfields"], sub_obj, ctx))
            else:
                out.append(item)
        return out
    raise ValidationFailed(
        f"field {obj_type.name}.{field_name} has no sub-selections",
        field="query",
    )


@dataclass(frozen=True, slots=True)
class _PartialError:
    """A per-field error sentinel returned by :func:`_resolve_selection`.

    The object builder replaces ``_PartialError`` instances with
    ``None`` and adds the error entry to the result's ``errors``
    list. The path the error is recorded under is the JSON path of
    the failing field (``case.documents``, ``cases.0.dataFields``, …)
    so the client can localize the denial.
    """

    code: str
    message: str


def _strip_non_null(type_: GraphQLType) -> GraphQLType:
    """Strip NON_NULL wrappers but keep LIST.

    Used to determine whether a value is a single object, a list of
    objects, or a scalar at the recursion decision point. Contrast
    with :func:`_unwrap` which strips both.
    """
    while type_.kind is GraphQLTypeKind.NON_NULL:
        assert type_.inner is not None
        type_ = type_.inner
    return type_


async def _resolve_object(
    parent: Mapping[str, Any],
    selections: tuple[dict[str, Any], ...],
    obj_type: GraphQLObjectType,
    ctx: ResolverContext,
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve an OBJECT type: build a dict from the requested fields.

    Fields whose resolver returned a ``_PartialError`` sentinel are
    emitted as ``None`` and the error is appended to the result's
    errors list with the full JSON path. The shape of the parent
    object is preserved so a client cannot infer the existence of
    a denied field from a missing key.
    """
    out: dict[str, Any] = {}
    for sel in selections:
        value = await _resolve_selection(selection=sel, parent=parent, obj_type=obj_type, ctx=ctx)
        if isinstance(value, _PartialError):
            _record_partial_error(
                ctx, code=value.code, message=value.message, path=path + (sel["name"],)
            )
            out[sel["name"]] = None
        else:
            out[sel["name"]] = value
    return out


def _lookup_field(obj_type: GraphQLObjectType, name: str) -> GraphQLField:
    """Return the field declaration for ``name``; raise on unknown field."""
    for fld in obj_type.fields:
        if fld.name == name:
            return fld
    raise ValidationFailed(
        f"unknown field {obj_type.name}.{name}",
        field="query",
    )


def _unwrap(type_: GraphQLType) -> GraphQLType:
    """Strip ``NON_NULL`` and ``LIST`` wrappers to reach the underlying type."""
    while type_.kind in {GraphQLTypeKind.NON_NULL, GraphQLTypeKind.LIST}:
        assert type_.inner is not None
        type_ = type_.inner
    return type_


async def _resolve_root(
    parsed: _ParsedQuery,
    root_field: GraphQLField,
    ctx: ResolverContext,
    root_field_name: str,
) -> tuple[Any, int | None]:
    """Dispatch the root selection and normalize the data branch.

    Returns ``(branch_value, result_count)`` where ``result_count``
    is 1/0 for ``case``, ``len`` for ``cases``, and ``None`` when the
    branch is not countable. Keeping this out of :func:`execute`
    holds the executor under the statement budget (ruff PLR0915)
    while leaving the error-to-envelope mapping in one place.
    """
    resolver_fn = _RESOLVERS[root_field.resolver]
    root_value = await resolver_fn(None, parsed.root_args, ctx)
    inner_type = _strip_non_null(root_field.type)
    if (
        inner_type.kind is GraphQLTypeKind.OBJECT
        and root_value is not None
        and isinstance(root_value, Mapping)
    ):
        sub_obj = SCHEMA_MODEL[inner_type.object_name]
        resolved = await _resolve_object(
            root_value,
            parsed.selections,
            sub_obj,
            ctx,
            path=(root_field_name,),
        )
        return resolved, 1
    if inner_type.kind is GraphQLTypeKind.LIST and isinstance(root_value, list):
        element_type = _strip_non_null(inner_type.inner)
        sub_obj_name = element_type.object_name
        assert sub_obj_name is not None
        sub_obj = SCHEMA_MODEL[sub_obj_name]
        out: list[Any] = []
        for idx, item in enumerate(root_value):
            if isinstance(item, Mapping):
                out.append(
                    await _resolve_object(
                        item,
                        parsed.selections,
                        sub_obj,
                        ctx,
                        path=(root_field_name, str(idx)),
                    )
                )
            else:
                out.append(item)
        return out, len(out)
    # why: ``None`` root (missing / cross-tenant id resolved to None
    # by the use case) normalizes to explicit null so the shape is stable.
    return root_value, (0 if root_value is None else 1)


async def execute(query: str, ctx: ResolverContext) -> GraphQLResult:
    """Run a governed GraphQL query and return the wire-shape result.

    The executor is intentionally small: it parses the query,
    dispatches the root selection against ``QUERY_TYPE``, and
    recursively resolves sub-selections. Errors from the application
    layer (Forbidden / NotFound / ValidationFailed) become entries
    in the errors list; the data graph is built only for fields the
    policy authorized.

    A denied authorization request produces a single error entry with
    ``code: forbidden`` and the field path that raised it. The data
    graph for that branch is ``null`` (never a partial object) so a
    leaked id cannot be inferred from a partial response.

    Every invocation appends exactly one PHI-free entry to
    ``ctx.audit_trail`` (see :func:`build_query_shape_audit`) so the
    HTTP transport can persist a ``GraphQLQueryExecuted`` audit row.
    """
    validate_schema_wiring()
    # why: clear per-request state first so a reused context (tests,
    # long-lived transports) never leaks the previous request's
    # partial errors into this response's ``errors`` array.
    ctx.partial_errors.clear()
    parsed: _ParsedQuery | None = None
    errors: list[dict[str, Any]] = []
    data: dict[str, Any] = {}
    root_field_name = "unknown"
    try:
        parsed = parse_query(query)
    except ValidationFailed as exc:
        # why: parse failures (malformed syntax, ``mutation``,
        # variables, fragments, oversize body) never reach a resolver.
        # The audit trail still records one ``unknown``-root entry so
        # every request is observable, then the error re-raises so the
        # HTTP transport maps it to 400 (transport contract) instead
        # of a 200 envelope.
        _record_audit(
            ctx,
            parsed=None,
            errors=[_error("validation_failed", str(exc), path=[])],
            result_count=None,
        )
        raise

    # Dispatch on the root field name. The schema declares only two
    # root fields today; an unknown root is rejected here as a
    # transport-level 400 (like a parse failure) so a probing client
    # cannot distinguish "unknown root" from "malformed query".
    root_field_name = parsed.root_field
    try:
        root_field = _lookup_field(QUERY_TYPE, root_field_name)
    except ValidationFailed as exc:
        _record_audit(
            ctx,
            parsed=parsed,
            errors=[_error("validation_failed", str(exc), path=[root_field_name])],
            result_count=None,
        )
        raise

    result_count: int | None = None
    try:
        data[root_field_name], result_count = await _resolve_root(
            parsed, root_field, ctx, root_field_name
        )
    except Forbidden as exc:
        # why: a root-level denial must not leave a partial object.
        # The branch is explicit null plus one error entry; sibling
        # state from a partially built object is discarded because
        # ``data`` was never assigned for this root.
        errors.append(_error("forbidden", str(exc), path=[root_field_name]))
        data[root_field_name] = None
        result_count = 0
    except NotFound as exc:
        # A missing or cross-tenant id surfaces as ``null`` in the
        # data graph plus a not_found error. The path points to the
        # root so the client can detect the missing reference.
        errors.append(_error("not_found", str(exc), path=[root_field_name]))
        data[root_field_name] = None
        result_count = 0
    except ValidationFailed as exc:
        errors.append(_error("validation_failed", str(exc), path=[root_field_name]))
        data = {}
        result_count = None
    # Promote per-field partial errors to the response. These were
    # captured at sub-selection depth (e.g. ``case.documents``
    # denied); the root try/except never saw them. The denied field
    # itself is already ``None``; sibling authorized fields are
    # preserved (GraphQL-spec shape) but no denied VALUE is emitted.
    errors.extend(ctx.partial_errors)
    _record_audit(ctx, parsed=parsed, errors=errors, result_count=result_count)
    return GraphQLResult(data=data, errors=tuple(errors))


def _record_audit(
    ctx: ResolverContext,
    *,
    parsed: _ParsedQuery | None,
    errors: list[dict[str, Any]],
    result_count: int | None,
) -> None:
    """Append one PHI-free query-shape entry to ``ctx.audit_trail``.

    The entry carries the actor, tenant, correlation id, and the
    safe shape from :func:`build_query_shape_audit`. Argument values,
    ids, and result data are never recorded (Constitution V).
    """
    payload = build_query_shape_audit(parsed=parsed, errors=errors, result_count=result_count)
    ctx.audit_trail.append(
        {
            "event_type": GRAPHQL_QUERY_EXECUTED,
            "tenant_id": str(ctx.tenant_id),
            "actor_id": str(ctx.actor_id),
            "correlation_id": ctx.correlation_id,
            **payload,
        }
    )


def _error(code: str, message: str, path: list[str]) -> dict[str, Any]:
    """Build a GraphQL-spec-shaped error entry."""
    return {
        "code": code,
        "message": message,
        "path": path,
    }
