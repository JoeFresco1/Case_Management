"""Governed query parser, complexity bounds, and safe query-shape audit (P13-US11)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.audit import GRAPHQL_QUERY_EXECUTED
from app.domain.errors import ValidationFailed
from app.interfaces.graphql.resolvers.context import (
    MAX_DEPTH,
    MAX_QUERY_LENGTH,
    MAX_SELECTIONS,
)

# ---------------------------------------------------------------------------
# Query parser + executor
#
# The parser is deliberately tiny — it understands the canonical
# ``query { case(id: ...) { ... } cases(limit: ..., offset: ...) { ... } }``
# shape. Anything more complex is rejected with a ValidationFailed.
# ---------------------------------------------------------------------------


# Tokens: identifiers, integers, strings, punctuation.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_INT = re.compile(r"-?\d+")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True, slots=True)
class _ParsedQuery:
    root_field: str
    root_args: dict[str, Any]
    selections: tuple[dict[str, Any], ...]


def _tokenize(query: str) -> list[str]:
    """Lex the GraphQL query into a flat token list.

    Recognizes:
    - Identifiers (``case``, ``id``, ``offset` ````, ``limit``, ``true``,
      ``false``, ``null``)
    - Integers (no floats — the schema does not expose any)
    - Strings (``"…"``)
    - Punctuation: ``{``, ``}``, ``(``, ``)``, ``:``, ``!``, ``,``, ``$``

    Whitespace is dropped. Anything that doesn't match is rejected
    with ``ValidationFailed`` so a malformed query never reaches the
    application layer.
    """
    tokens: list[str] = []
    pos = 0
    n = len(query)
    while pos < n:
        ch = query[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch in "{}():,!$":
            tokens.append(ch)
            pos += 1
            continue
        m = _INT.match(query, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        m = _STR.match(query, pos)
        if m:
            tokens.append(f'"{m.group(1)}"')
            pos = m.end()
            continue
        m = _IDENT.match(query, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        raise ValidationFailed(
            f"unexpected character at offset {pos}: {query[pos]!r}",
            field="query",
        )
    return tokens


def _parse_value(tokens: list[str], idx: int) -> tuple[Any, int]:
    """Parse a GraphQL value starting at ``tokens[idx]``.

    Supports: ``String``, ``Int``, ``Boolean``, ``null``, and variable
    references (which are resolved at the executor layer). Returns
    ``(value, new_idx)``.
    """
    tok = tokens[idx]
    if tok.startswith('"'):
        return tok[1:-1], idx + 1
    if tok == "true":
        return True, idx + 1
    if tok == "false":
        return False, idx + 1
    if tok == "null":
        return None, idx + 1
    if re.fullmatch(r"-?\d+", tok):
        return int(tok), idx + 1
    raise ValidationFailed(f"unsupported literal: {tok}", field="query")


def _parse_args(tokens: list[str], idx: int) -> tuple[dict[str, Any], int]:
    """Parse ``(name: value, ...)`` starting at the ``(`` token."""
    if tokens[idx] != "(":
        return {}, idx
    idx += 1  # skip ``(``
    args: dict[str, Any] = {}
    while tokens[idx] != ")":
        name = tokens[idx]
        idx += 1
        if tokens[idx] != ":":
            raise ValidationFailed("expected ':' after arg name", field="query")
        idx += 1
        value, idx = _parse_value(tokens, idx)
        args[name] = value
        if tokens[idx] == ",":
            idx += 1
    return args, idx + 1  # skip ``)``


def _parse_selection(tokens: list[str], idx: int) -> tuple[dict[str, Any], int]:
    """Parse a single selection ``name(args) { sub1 sub2 ... }``.

    Returns ``{"name": name, "args": {...}, "subfields": [...]}``.
    """
    name = tokens[idx]
    idx += 1
    args, idx = _parse_args(tokens, idx)
    if idx >= len(tokens) or tokens[idx] != "{":
        return {"name": name, "args": args, "subfields": ()}, idx
    idx += 1  # skip ``{``
    subfields: list[dict[str, Any]] = []
    while tokens[idx] != "}":
        sub, idx = _parse_selection(tokens, idx)
        subfields.append(sub)
    return {"name": name, "args": args, "subfields": tuple(subfields)}, idx + 1


def parse_query(query: str) -> _ParsedQuery:
    """Parse a GraphQL query string into the internal selection tree.

    Only the canonical root form is supported:
    ``query { <root>(...) { ... } }``. ``mutation``, fragments,
    aliases, and variables are deliberately rejected.
    """
    # why: the executor recurses per selection with no streaming, so
    # an unbounded body pins the event loop before the parser even
    # runs. The HTTP transport enforces the same bound via Pydantic;
    # direct ``execute()`` callers get it here.
    if len(query) > MAX_QUERY_LENGTH:
        raise ValidationFailed(
            f"query exceeds maximum length of {MAX_QUERY_LENGTH}",
            field="query",
        )
    _reject_unsupported_syntax(query)
    tokens = _tokenize(query.strip())
    if not tokens:
        raise ValidationFailed("empty query", field="query")
    # Optional ``query`` keyword.
    idx = 0
    if tokens[idx] == "query":
        idx += 1
    if idx >= len(tokens) or tokens[idx] != "{":
        raise ValidationFailed("query must start with 'query {'", field="query")
    idx += 1  # skip outer ``{``
    selection, idx = _parse_selection(tokens, idx)
    # ``_parse_selection`` consumes the ``}`` of the selection's
    # selection set; we still need to consume the outer query
    # block's ``}``.
    if idx >= len(tokens) or tokens[idx] != "}":
        raise ValidationFailed("missing closing '}' of query block", field="query")
    idx += 1
    if idx != len(tokens):
        raise ValidationFailed("trailing tokens after root selection", field="query")
    parsed = _ParsedQuery(
        root_field=selection["name"],
        root_args=selection["args"],
        selections=selection["subfields"],
    )
    _enforce_complexity_bounds(parsed)
    return parsed


def _reject_unsupported_syntax(query: str) -> None:
    """Reject operations the governed surface never executes.

    The checks run on the raw text (before lexing) so a ``mutation``
    or ``subscription`` keyword fails with an explainable message
    instead of a generic ``unknown field`` error. Introspection
    (``__schema`` / ``__type`` / any ``__``-prefixed identifier) and
    variable references (``$``) are rejected for the same reason:
    the executor inlines all arguments and exposes no introspection.
    Fragment syntax (``...`` / the ``fragment`` keyword) is rejected
    because the parser has no fragment environment.
    """
    lowered = query.strip().lower()
    if lowered.startswith("mutation"):
        raise ValidationFailed(
            "mutations are not supported by the governed surface; use the REST API",
            field="query",
        )
    if lowered.startswith("subscription"):
        raise ValidationFailed(
            "subscriptions are not supported by the governed surface",
            field="query",
        )
    if "$" in query:
        raise ValidationFailed(
            "variables are not supported by the governed surface; inline all arguments",
            field="query",
        )
    if "..." in query or "fragment" in lowered.split():
        raise ValidationFailed(
            "fragments are not supported by the governed surface",
            field="query",
        )
    if "__" in query:
        raise ValidationFailed(
            "introspection is not supported by the governed surface",
            field="query",
        )


def _count_selections(selections: tuple[dict[str, Any], ...]) -> int:
    """Count every selected field in the tree (recursive)."""
    total = 0
    for sel in selections:
        total += 1
        total += _count_selections(sel.get("subfields", ()))
    return total


def _selection_depth(selections: tuple[dict[str, Any], ...]) -> int:
    """Return the maximum nesting depth below the root (root = 0)."""
    if not selections:
        return 0
    return 1 + max((_selection_depth(sel.get("subfields", ())) for sel in selections), default=0)


def _enforce_complexity_bounds(parsed: _ParsedQuery) -> None:
    """Reject queries whose shape exceeds the governed budget.

    why: each nested selection is at most one repository round-trip
    (``Case.documents``) or one redaction pass (``Case.dataFields``).
    An unbounded selection set turns one HTTP request into an
    N+1 fan-out. The budget (30 fields, depth 4) covers the
    canonical Case + DataFields + Documents read with headroom while
    keeping the per-request cost trivial.
    """
    total = 1 + _count_selections(parsed.selections)
    if total > MAX_SELECTIONS:
        raise ValidationFailed(
            f"query selects {total} fields; maximum is {MAX_SELECTIONS}",
            field="query",
        )
    depth = _selection_depth(parsed.selections)
    if depth > MAX_DEPTH:
        raise ValidationFailed(
            f"query depth {depth} exceeds maximum of {MAX_DEPTH}",
            field="query",
        )


def describe_query_shape(parsed: _ParsedQuery | None) -> dict[str, Any]:
    """Project a parsed query to its safe, auditable shape.

    The shape carries field NAMES and the pagination window only —
    never argument values (ids), raw text, or result data — so the
    audit row is safe to retain under Constitution V. ``limit`` /
    ``offset`` are numbers (not content); the ``case(id:)`` argument
    is projected as the ``ref`` marker. An unparsed query (parse
    failure) yields ``{"root": "unknown", ...}`` so every request
    still leaves exactly one audit entry.
    """
    if parsed is None:
        return {
            "root": "unknown",
            "fields": [],
            "selection_count": 0,
            "depth": 0,
            "limit": None,
            "offset": None,
        }
    names: list[str] = []

    def _walk(selections: tuple[dict[str, Any], ...]) -> None:
        for sel in selections:
            name = str(sel.get("name", ""))
            if name:
                names.append(name)
            _walk(sel.get("subfields", ()))

    _walk(parsed.selections)
    shape: dict[str, Any] = {
        "root": parsed.root_field,
        "fields": sorted(set(names)),
        "selection_count": 1 + _count_selections(parsed.selections),
        "depth": _selection_depth(parsed.selections),
        "limit": None,
        "offset": None,
    }
    # why: pagination numbers are safe to retain (they describe the
    # window, not content). The ``id`` argument value is never
    # retained — its presence is implied by ``root == "case"``.
    raw_limit = parsed.root_args.get("limit")
    raw_offset = parsed.root_args.get("offset")
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
        shape["limit"] = raw_limit
    if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
        shape["offset"] = raw_offset
    return shape


def build_query_shape_audit(
    *,
    parsed: _ParsedQuery | None,
    errors: list[dict[str, Any]],
    result_count: int | None,
) -> dict[str, Any]:
    """Build the PHI-free payload for a ``GraphQLQueryExecuted`` audit row.

    ``errors`` contribute outcome codes only (``forbidden`` /
    ``not_found`` / ``validation_failed``); messages are excluded
    because a ``NotFound`` message echoes the caller-supplied id.
    ``result_count`` is the number of top-level rows (1/0 for
    ``case``, ``len`` for ``cases``, ``None`` when parse failed).
    """
    shape = describe_query_shape(parsed)
    codes = sorted({str(e.get("code", "")) for e in errors if e.get("code")})
    shape["event_type"] = GRAPHQL_QUERY_EXECUTED
    shape["outcome"] = "ok" if not codes else "denied" if "forbidden" in codes else "error"
    shape["error_codes"] = codes
    shape["result_count"] = result_count
    return shape
