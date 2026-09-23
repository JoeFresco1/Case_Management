"""Governed GraphQL schema (P13-US11 / FM-057).

The platform exposes a thin, **governed** GraphQL surface for ad-hoc
queries. Governance here means every resolver is policy-enforced and
routes through the application service layer — there is no path from a
GraphQL field straight to a repository, document store, or domain
object that bypasses the existing authorization + tenant + field +
document boundaries.

# Why hand-rolled (not strawberry / ariadne / graphene)

AGENTS.md §4.R3: "Prefer boring over clever." Adding a third-party
GraphQL framework would (a) pull a new dependency into ``pyproject``
which the constitution + ADR-001 portable-types posture ask us to
keep minimal, and (b) require new framework-specific abstractions in
the resolvers that the rest of the platform does not use. The surface
required for FM-057 is narrow (a Case + Document read), so the
parser + executor here stays small and uses only the Python stdlib +
the existing application layer.

# Boundaries (Constitution V / AGENTS.md §4.R12)

1. **Service boundary** — every resolver delegates to a use-case
   function in ``app.application.*``. Resolvers never read or write
   a repository directly. Mutation operations are intentionally
   out of scope for the governed surface: GraphQL clients must use
   the REST API for writes so the existing idempotency, optimistic
   concurrency, and audit + outbox transaction guarantees are
   preserved.

2. **Tenant boundary** — the actor's ``tenant_id`` is the only
   tenant_id that ever reaches a repository method. The schema does
   not expose a ``tenantId`` argument; tenant isolation is implicit
   in the actor's identity. Cross-tenant queries never enter the
   application layer because the repository filters reject them.

3. **Field boundary** — every case data field is gated by the
   published ``CaseTypeVersion.schema.sensitive_field_keys()``. The
   policy is evaluated once per request; the resolver passes the
   resulting ``can_see_sensitive`` boolean into
   :func:`app.application.cases.redactor.redact_case_data`. The wire
   shape never includes a redacted value.

4. **Document boundary** — the ``documents`` connection goes
   through the document application layer (which calls
   ``Action.DOCUMENT_READ``) and respects the case-type's
   ``document_classifications`` allowlist. Locked documents remain
   readable (the read action is distinct from the lock and redact
   actions) but redactions + new versions are not surfaced through
   GraphQL — clients use the dedicated endpoints for those.

# Authoritative reference

The schema is a *catalog* of permitted shapes. The resolver module
``resolvers.py`` implements each field as a single call into the
application layer. New fields land here as a schema entry + a
resolver function in the same change so the boundary stays obvious.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# Type system
#
# This is a minimal type system — enough to express the canonical Case +
# Document read shape that FM-057 requires. It is **not** a full GraphQL
# implementation: introspection, fragments, aliases, variables, and
# subscriptions are deliberately out of scope. Clients that need them
# use the REST API (AGENTS.md §10).
# ---------------------------------------------------------------------------


class GraphQLTypeKind(StrEnum):
    """The category of a governed type.

    ``SCALAR`` — a leaf value (String, ID, Boolean, Int, Enum).
    ``OBJECT`` — a composite type whose fields are resolved via the
    application layer.
    ``LIST`` — a list of one inner type. ``LIST[OBJECT]`` is the
    only list shape supported; ``LIST[LIST]`` and ``LIST[NULL]`` are
    rejected by the executor.
    ``NON_NULL`` — wraps an inner type that may not be null. ``NON_NULL``
    chains compose: ``NON_NULL[NON_NULL[X]]`` is supported.
    """

    SCALAR = "SCALAR"
    OBJECT = "OBJECT"
    LIST = "LIST"
    NON_NULL = "NON_NULL"


class ScalarKind(StrEnum):
    """Supported scalar kinds.

    ``ID`` — UUID strings (the platform uses ``uuid4`` / ``uuid7``
    exclusively; we never expose raw SQL row ids).
    ``String`` / ``Int`` / ``Boolean`` / ``DateTime`` — see the wire
    schemas in ``interfaces.http.schemas.cases``.
    ``Enum`` — backed by the application's ``Action`` / ``CaseStatus``
    vocabularies; unknown values are rejected at parse time.
    """

    ID = "ID"
    STRING = "String"
    INT = "Int"
    BOOLEAN = "Boolean"
    DATETIME = "DateTime"
    ENUM = "Enum"


@dataclass(frozen=True, slots=True)
class GraphQLType:
    """A type reference in the governed schema.

    ``kind`` is the wrapper shape; ``inner`` is the wrapped type when
    ``kind in {LIST, NON_NULL}``. ``scalar`` and ``enum_values`` describe
    the leaf when ``kind is SCALAR``.
    """

    kind: GraphQLTypeKind
    inner: GraphQLType | None = None
    scalar: ScalarKind | None = None
    enum_values: tuple[str, ...] = ()
    object_name: str | None = None


# Convenience constructors. Kept module-level so resolvers and tests
# share the same shape.


def _id() -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.ID)


def _string() -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.STRING)


def _int() -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.INT)


def _bool() -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.BOOLEAN)


def _datetime() -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.DATETIME)


def _enum(values: tuple[str, ...]) -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.SCALAR, scalar=ScalarKind.ENUM, enum_values=values)


def _list(inner: GraphQLType) -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.LIST, inner=inner)


def _non_null(inner: GraphQLType) -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.NON_NULL, inner=inner)


def _object(name: str) -> GraphQLType:
    return GraphQLType(kind=GraphQLTypeKind.OBJECT, object_name=name)


# ---------------------------------------------------------------------------
# Field declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphQLField:
    """A field on an ``OBJECT`` type.

    ``name`` is the GraphQL field name; ``type`` is its declared type;
    ``resolver`` is the dotted path the executor calls (resolved via
    :func:`resolvers.resolve_field`). ``description`` is informational
    and surfaced in the introspection dump that the operator console
    uses to verify which fields are exposed (P11 / transparency).
    """

    name: str
    type: GraphQLType
    resolver: str
    description: str


@dataclass(frozen=True, slots=True)
class GraphQLObjectType:
    """An ``OBJECT`` type and its declared fields.

    ``name`` is the GraphQL type name (PascalCase per GraphQL
    convention). Fields are addressable by name; the executor looks up
    each requested field against this map and rejects unknown fields.
    """

    name: str
    fields: tuple[GraphQLField, ...]
    description: str


# ---------------------------------------------------------------------------
# Schema catalog
# ---------------------------------------------------------------------------


# Case-status enum values mirror ``app.domain.case.CaseStatus``.
CASE_STATUS_ENUM: Final[tuple[str, ...]] = (
    "draft",
    "open",
    "in_progress",
    "on_hold",
    "closed",
    "cancelled",
)

# Document-sensitivity enum values mirror ``app.domain.document.DocumentSensitivity``.
DOCUMENT_SENSITIVITY_ENUM: Final[tuple[str, ...]] = (
    "public",
    "internal",
    "confidential",
    "restricted",
    "phi",
)


# ``Case`` — the governed read shape. Every field goes through
# :mod:`app.application.cases` resolvers; no field reads the
# repository directly. The ``data`` field is intentionally absent
# from the schema at this level: per-field redaction is exposed via
# the ``dataFields`` field below so the policy evaluation is
# observable in the introspection.
CASE_TYPE: Final[GraphQLObjectType] = GraphQLObjectType(
    name="Case",
    description=(
        "An authorized read view of a Case. Tenant + role boundaries are "
        "enforced by the resolver layer; clients never supply a tenantId."
    ),
    fields=(
        GraphQLField(
            name="id",
            type=_non_null(_id()),
            resolver="case_id",
            description="Stable Case id (UUID).",
        ),
        GraphQLField(
            name="reference",
            type=_non_null(_string()),
            resolver="case_reference",
            description="Human-readable Case reference (C-{tenant}-{YYYY}-{seq}).",
        ),
        GraphQLField(
            name="status",
            type=_non_null(_enum(CASE_STATUS_ENUM)),
            resolver="case_status",
            description="Current case status.",
        ),
        GraphQLField(
            name="priority",
            type=_non_null(_string()),
            resolver="case_priority",
            description="Case priority (low|normal|high|urgent).",
        ),
        GraphQLField(
            name="title",
            type=_non_null(_string()),
            resolver="case_title",
            description="Case title (always non-sensitive).",
        ),
        GraphQLField(
            name="description",
            type=_non_null(_string()),
            resolver="case_description",
            description="Case description (always non-sensitive).",
        ),
        GraphQLField(
            name="sensitivity",
            type=_non_null(_string()),
            resolver="case_sensitivity",
            description=(
                "Effective case sensitivity label. Restricted/PHI cases are "
                "still readable; the per-field redaction is applied in "
                "``dataFields``."
            ),
        ),
        GraphQLField(
            name="dataFields",
            type=_non_null(_list(_non_null(_object("DataField")))),
            resolver="case_data_fields",
            description=(
                "Per-field structured payload with policy-driven redaction. "
                "Sensitive fields are returned as ``{redacted: true, reason: "
                '"sensitive"}`` when the actor lacks the sensitive-read '
                "grant; non-sensitive fields are returned verbatim. This is "
                "the ONLY way to read structured case data through GraphQL."
            ),
        ),
        GraphQLField(
            name="documents",
            type=_non_null(_list(_non_null(_object("Document")))),
            resolver="case_documents",
            description=(
                "Documents attached to the case. Each entry is gated by "
                "Action.DOCUMENT_READ and respects the case-type's "
                "document_classifications allowlist. Locked documents are "
                "readable; mutations (new version / redaction) are not "
                "exposed through GraphQL — use the REST endpoints."
            ),
        ),
    ),
)


# ``DataField`` — a single key/value pair from the case's structured
# payload. The key is always present (preserved on redaction); the
# value is the actual data or the redaction marker.
DATA_FIELD_TYPE: Final[GraphQLObjectType] = GraphQLObjectType(
    name="DataField",
    description=(
        "One field of the case's structured payload. ``value`` is either "
        "the original data or ``[REDACTED]`` when policy applies. "
        "``redacted`` is the boolean the client localizes on."
    ),
    fields=(
        GraphQLField(
            name="key",
            type=_non_null(_string()),
            resolver="data_field_key",
            description="Field key (matches the CaseTypeVersion schema).",
        ),
        GraphQLField(
            name="value",
            type=_non_null(_string()),
            resolver="data_field_value",
            description=(
                "Field value. Replaced by the literal ``[REDACTED]`` when "
                "policy applies; that is the only string a client sees for "
                "a sensitive field."
            ),
        ),
        GraphQLField(
            name="redacted",
            type=_non_null(_bool()),
            resolver="data_field_redacted",
            description=(
                "``True`` iff the field was replaced by policy. The "
                "client uses this to localize the rendering."
            ),
        ),
    ),
)


# ``Document`` — read shape for case-attached documents.
DOCUMENT_TYPE: Final[GraphQLObjectType] = GraphQLObjectType(
    name="Document",
    description=(
        "Read view of a case-attached Document. ``contentBytes`` is never "
        "exposed: document bytes are accessed through the dedicated REST "
        "endpoint so the ``Content-Disposition`` header and audit row can "
        "be set in one transaction."
    ),
    fields=(
        GraphQLField(
            name="id",
            type=_non_null(_id()),
            resolver="document_id",
            description="Stable Document id (UUID).",
        ),
        GraphQLField(
            name="fileName",
            type=_non_null(_string()),
            resolver="document_file_name",
            description="Original file name.",
        ),
        GraphQLField(
            name="classification",
            type=_non_null(_string()),
            resolver="document_classification",
            description=(
                "Document classification (e.g. Contract, Correspondence, "
                "Evidence). The value is always one of the case-type's "
                "declared ``document_classifications``; an undeclared "
                "classification surfaces as NotFound rather than leaking."
            ),
        ),
        GraphQLField(
            name="sensitivity",
            type=_non_null(_enum(DOCUMENT_SENSITIVITY_ENUM)),
            resolver="document_sensitivity",
            description="Document sensitivity label.",
        ),
        GraphQLField(
            name="locked",
            type=_non_null(_bool()),
            resolver="document_locked",
            description=(
                "``True`` iff the document is locked against further "
                "mutations. Locked documents remain readable."
            ),
        ),
        GraphQLField(
            name="currentVersion",
            type=_non_null(_int()),
            resolver="document_current_version",
            description="Current DocumentVersion number.",
        ),
        GraphQLField(
            name="uploadedAt",
            type=_non_null(_datetime()),
            resolver="document_uploaded_at",
            description="ISO-8601 upload timestamp (UTC).",
        ),
    ),
)


# Catalog — ``Query`` is the root type. ``case(id: ID!)`` returns a
# single Case; ``cases(limit: Int, offset: Int)`` returns a paginated
# list.
QUERY_TYPE: Final[GraphQLObjectType] = GraphQLObjectType(
    name="Query",
    description=(
        "Root query surface. Every field is policy-enforced. Mutation is "
        "intentionally absent: writes go through the REST API so "
        "idempotency, optimistic concurrency, and audit + outbox "
        "guarantees are preserved in one transactional boundary."
    ),
    fields=(
        GraphQLField(
            name="case",
            type=_object("Case"),
            resolver="query_case",
            description=(
                "Look up a single Case by id within the actor's tenant. "
                "Returns ``null`` for missing or cross-tenant ids — the "
                "two cases are intentionally indistinguishable so a "
                "leaked id cannot be detected by a side channel."
            ),
        ),
        GraphQLField(
            name="cases",
            type=_non_null(_list(_non_null(_object("Case")))),
            resolver="query_cases",
            description=(
                "List cases in the actor's tenant, newest first. ``limit`` "
                "is clamped to ``[1, 200]``; ``offset`` to ``[0, ...)`` "
                "(AGENTS.md §17.1)."
            ),
        ),
    ),
)


# The complete catalog. The executor resolves unknown types against
# this dict and refuses any field that is not declared here. Adding a
# field means: (1) add the ``GraphQLField`` to the relevant
# ``GraphQLObjectType`` above; (2) add a resolver function in
# ``resolvers.py`` keyed by ``field.resolver``. The two changes ship
# in the same commit.
SCHEMA_MODEL: Final[dict[str, GraphQLObjectType]] = {
    obj.name: obj for obj in (CASE_TYPE, DATA_FIELD_TYPE, DOCUMENT_TYPE, QUERY_TYPE)
}


__all__ = [
    "CASE_STATUS_ENUM",
    "DOCUMENT_SENSITIVITY_ENUM",
    "CASE_TYPE",
    "DATA_FIELD_TYPE",
    "DOCUMENT_TYPE",
    "GraphQLField",
    "GraphQLObjectType",
    "GraphQLType",
    "GraphQLTypeKind",
    "QUERY_TYPE",
    "SCHEMA_MODEL",
    "ScalarKind",
]
