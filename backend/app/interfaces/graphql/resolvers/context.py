"""Resolver context, result envelope, governed bounds, and dispatch registry (P13-US11)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from app.domain.errors import ValidationFailed
from app.domain.ports import (
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
)

# ---------------------------------------------------------------------------
# Governed bounds (P13-US11 / FM-057 hardening).
#
# why: the executor is a hand-rolled recursive parser with no streaming,
# so an unbounded body, selection set, or page window would let one
# caller pin the event loop or exfiltrate the tenant row by row. The
# bounds below mirror the REST contract (AGENTS.md section 17.1:
# limit default 50 max 200) and the HTTP transport's MAX_QUERY_LENGTH
# so direct ``execute()`` callers get the same guarantees as HTTP.
# ---------------------------------------------------------------------------

MAX_QUERY_LENGTH: Final[int] = 8000
MAX_SELECTIONS: Final[int] = 30
MAX_DEPTH: Final[int] = 4
MAX_PAGE_LIMIT: Final[int] = 200
DEFAULT_PAGE_LIMIT: Final[int] = 50

# ---------------------------------------------------------------------------
# Context passed into every resolver
# ---------------------------------------------------------------------------


@dataclass
class ResolverContext:
    """The runtime context for a single GraphQL request.

    The transport layer constructs this once per request from the
    request scope and passes it into :func:`execute`. Every
    resolver receives this context; the actor's ``tenant_id`` is the
    only tenant identifier that ever reaches a repository call.

    ``correlation_id`` is logged + recorded in the audit row.
    ``partial_errors`` is a list the executor appends to for
    per-field denials (Forbidden / NotFound at sub-selection depth);
    the root-level error handling in :func:`execute` reads the list
    at the end of the request to assemble the response.
    """

    actor_id: UUID
    tenant_id: UUID
    actor_roles: tuple[str, ...]
    case_repo: CaseRepository
    case_type_version_repo: CaseTypeVersionRepository
    document_repo: DocumentRepository
    correlation_id: str = ""
    partial_errors: list[dict[str, Any]] = field(default_factory=list)
    # why: the executor records one safe query-shape entry per request
    # (root + field names + outcome codes, never argument values, ids,
    # or result data) so the HTTP transport can persist a
    # ``GraphQLQueryExecuted`` audit row without the resolver layer
    # importing infrastructure. Tests assert on this list directly.
    audit_trail: list[dict[str, Any]] = field(default_factory=list)


def _record_partial_error(
    ctx: ResolverContext, *, code: str, message: str, path: tuple[str, ...]
) -> None:
    """Append a per-field error entry to ``ctx.partial_errors``.

    The path is the JSON path of the failing field (``case.documents``
    or ``cases.0.dataFields``). The executor promotes this list to
    the response's ``errors`` array at the end of the request.
    """
    ctx.partial_errors.append({"code": code, "message": message, "path": list(path)})


def _reject_unknown_args(args: Mapping[str, Any], *, allowed: set[str], root: str) -> None:
    """Fail closed on undeclared root arguments.

    The governed surface inlines every argument at parse time. An
    undeclared argument (``cases(foo: 1)``) would otherwise be
    silently dropped, letting a caller believe a filter ran when it
    did not.
    """
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ValidationFailed(
            f"unknown argument(s) for {root}: {', '.join(unknown)}",
            field="query",
        )


def _coerce_pagination_int(value: Any, *, name: str) -> int:
    """Coerce a pagination argument to ``int`` or raise ``ValidationFailed``.

    ``bool`` is rejected explicitly (it subclasses ``int`` but is
    never a meaningful page window). Floats, strings, and ``None``
    are rejected rather than truncated so a misconfigured client
    fails loud instead of paging an unintended window.
    """
    if isinstance(value, bool):
        raise ValidationFailed(f"{name} must be an integer", field=name)
    if not isinstance(value, int):
        raise ValidationFailed(f"{name} must be an integer", field=name)
    return value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphQLResult:
    """The wire-shape result of a governed GraphQL request.

    ``data`` is the resolved dict (matches the GraphQL spec for a
    successful response). ``errors`` is the list of error dicts the
    spec defines for partial failures. A denied authorization
    request appears as an entry in ``errors`` with ``code: forbidden``
    and never as a leaked field value — the data graph is built
    only for fields the policy authorized.
    """

    data: dict[str, Any]
    errors: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# Resolver dispatch
# ---------------------------------------------------------------------------


# Map of ``GraphQLField.resolver`` -> callable. The dispatcher looks
# up the field's resolver key here; a missing entry is a wiring bug
# caught at startup (the schema-validation helper below errors when
# a declared field has no resolver).
_RESOLVERS: Final[dict[str, ResolverFn]] = {}


# Resolver signature contract — a callable that takes
# ``(parent, args, ctx)`` and returns a coroutine resolving to the
# field value. We keep a typed alias so mypy sees the call sites.
ResolverFn = Any  # noqa: F401 — exported for typing callers


def _register(name: str):
    """Decorator that registers a function in :data:`_RESOLVERS`.

    Functions wrapped here are coroutines that take
    ``(parent, args, ctx)`` and return the field value. ``parent``
    is the resolved object for the enclosing type (a ```` for a
    ``Case.dataFields`` call). ``args`` is the parsed argument dict;
    today only ``Query.case(id: ID!)`` and ``Query.cases(limit, offset)``
    carry arguments. ``ResolverContext`` carries the actor +
    repositories.
    """

    def deco(fn: ResolverFn) -> ResolverFn:
        _RESOLVERS[name] = fn
        return fn

    return deco
