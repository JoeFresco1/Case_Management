"""Access-decision audit recorder and report (P09-US6 / FM-045; P11-US9).

# INTEGRATION_SEAM: authorization-audit

Every authorization decision (allow and deny) is an audit event whose
payload carries the evaluated ``action``, the targeted ``resource``
(kind + id), the ``actor``, the ``result`` (allowed / denied), and the
policy ``reason``. The security administrator reads those rows through
``GET /api/v1/audit/access-decisions`` (P09-US6).

# Why a separate module

The access-decision recorder sits next to the audit ``record_event``
use case because every other audit row is the consequence of a domain
change. An authorization decision, by contrast, is the consequence of
**policy evaluation** — not of a successful business mutation. Keeping
the recorder separate keeps ``record_event`` focused on the
"exactly one audit row per domain change" contract (P09-US2) and lets
the access-decision audit be wired into the ``Policy`` without
forcing every use case to know about it.

# Why both allow and deny are recorded

A deny is a successful outcome of the policy engine ("the policy
correctly refused the action") and a security-relevant signal of its
own: cross-tenant probes and role-probing attempts must surface in
the report (P11-US2 / K1). A silent allow is just as audit-worthy
as a deny because the allow was a grant that a security
administrator can later revoke and audit.

# Same-transaction guarantee (P09-US2)

``record_decision`` does not commit. It stages the row on the caller's
session; the caller commits once. The HTTP layer wraps ``Policy``
evaluation in the same transaction as the use case's domain change
so a failed audit write rolls back the business decision. When the
policy is called outside a transaction (e.g. from a CLI), the caller
is responsible for the transaction boundary — the recorder never
commits on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.application.authorization import Action, Decision, Resource
from app.domain.audit import ACCESS_DECISION_RECORDED
from app.domain.context import Clock, UserContext
from app.domain.errors import ValidationFailed
from app.domain.ports import AuditEventStore

# ---------------------------------------------------------------------------
# Wire payload keys (P09-US6 / FM-045)
# ---------------------------------------------------------------------------

ACCESS_KEY_ACTION: Final[str] = "action"
ACCESS_KEY_RESOURCE_KIND: Final[str] = "resource_kind"
ACCESS_KEY_RESOURCE_ID: Final[str] = "resource_id"
ACCESS_KEY_RESULT: Final[str] = "result"
ACCESS_KEY_REASON: Final[str] = "reason"
ACCESS_KEY_ACTOR_ROLES: Final[str] = "actor_roles"

RESULT_ALLOWED: Final[str] = "allowed"
RESULT_DENIED: Final[str] = "denied"

VALID_RESULTS: Final[frozenset[str]] = frozenset({RESULT_ALLOWED, RESULT_DENIED})

# Default page size for the report (AGENTS.md §17.1).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200


# Protocol used by ``app.application.authorization.authorize`` to record
# the decision in the same transaction as the use case. The callable
# accepts the same inputs the policy evaluated plus the policy's
# Decision so the recorder never has to re-evaluate authorization.
AccessDecisionRecorder = Callable[
    [Action, UserContext, Resource, Decision, str],
    Awaitable[None],
]


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessDecisionInput:
    """The minimum information the policy needs to record a decision.

    Why a typed wrapper: the recorder is called from a hot path
    (every authorization check) so a flat dataclass with the exact
    fields it needs keeps the call site explicit. The caller passes
    the ``Decision`` returned by ``Policy.evaluate``; the recorder
    flips ``result`` between ``"allowed"`` and ``"denied"`` based on
    ``decision.allowed``.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_type: str
    actor_roles: tuple[str, ...]
    action: Action
    resource: Resource
    decision: Decision
    correlation_id: str
    causation_id: str | None
    occurred_at: datetime


def _validate_input(cmd: AccessDecisionInput) -> None:
    """Run the application-level invariants on ``cmd``.

    Why a separate validator: the recorder is invoked from the policy
    path so a missing actor id or unknown action enum must surface as a
    ``ValidationFailed`` before it reaches the store. The HTTP layer
    maps ``ValidationFailed`` to 422; the in-process path raises so a
    misconfigured caller fails fast.
    """
    if not isinstance(cmd.action, Action):
        raise ValidationFailed(f"unknown action: {cmd.action!r}", field="action")
    if cmd.actor_type not in {"user", "system", "service"}:
        raise ValidationFailed(f"unknown actor_type: {cmd.actor_type!r}", field="actor_type")
    if not cmd.resource.kind:
        raise ValidationFailed("resource.kind is required", field="resource")


async def record_decision(
    cmd: AccessDecisionInput,
    *,
    audit: AuditEventStore,
) -> None:
    """Append a single ``AccessDecisionRecorded`` row to the chain.

    Why this helper exists: every call site that wants to audit a
    policy decision should call exactly one function. The recorder
    builds the payload, normalizes the result string, and stages the
    append on the caller's session. It does not commit.

    Failure modes:
    - ``ValidationFailed`` — when ``cmd`` carries an unknown action
      enum or actor_type, or when ``resource.kind`` is empty.
    """
    _validate_input(cmd)
    result = RESULT_ALLOWED if cmd.decision.allowed else RESULT_DENIED
    payload: dict[str, Any] = {
        ACCESS_KEY_ACTION: cmd.action.value,
        ACCESS_KEY_RESOURCE_KIND: cmd.resource.kind,
        ACCESS_KEY_RESOURCE_ID: (str(cmd.resource.id) if cmd.resource.id is not None else None),
        ACCESS_KEY_RESULT: result,
        ACCESS_KEY_REASON: cmd.decision.reason,
        ACCESS_KEY_ACTOR_ROLES: list(cmd.actor_roles),
    }
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=ACCESS_DECISION_RECORDED,
        actor_id=cmd.actor_id,
        actor_type=cmd.actor_type,
        subject_type=cmd.resource.kind,
        subject_id=cmd.resource.id or cmd.actor_id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        payload=payload,
        occurred_at=cmd.occurred_at,
    )


def build_recorder(
    *,
    audit: AuditEventStore,
    clock: Clock,
    actor_lookup: Any | None = None,  # noqa: ARG001
) -> AccessDecisionRecorder:
    """Build a thin callable for the policy to invoke on every decision.

    Why a factory: the policy module is intentionally framework-free
    and must not import the audit recorder directly. ``deps.py`` calls
    this factory at startup, captures the callable, and the policy
    layer calls it through a Protocol. The result is dependency
    injection without coupling ``app.application.authorization`` to
    ``app.application.audit``.

    ``actor_lookup`` is unused in this slice; it is reserved for the
    future ``actor_roles`` enrichment (a tenant-local actor may carry
    roles that differ from the IdP claim). Kept in the signature so
    the policy integration does not need a refactor when the
    enrichment lands.
    """

    async def _record(
        action: Action,
        user: UserContext,
        resource: Resource,
        decision: Decision,
        correlation_id: str,
    ) -> None:
        await record_decision(
            AccessDecisionInput(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_type="user" if not user.is_system else "system",
                actor_roles=user.roles,
                action=action,
                resource=resource,
                decision=decision,
                correlation_id=correlation_id,
                causation_id=None,
                occurred_at=clock.now(),
            ),
            audit=audit,
        )

    return _record


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessDecisionEntry:
    """A single access-decision row on the security report wire shape.

    Mirrors the audit row's access-decision columns plus a redacted
    flag (always ``False`` for this event type: access decisions do
    not carry protected payloads — the sensitive material is the
    decision itself, which is what this endpoint returns).
    """

    id: UUID
    occurred_at: str
    actor_id: UUID
    actor_type: str
    actor_roles: list[str]
    action: str
    resource_kind: str
    resource_id: str | None
    result: str
    reason: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class AccessDecisionPage:
    """A page of access-decision rows plus the standard cursor envelope."""

    items: list[AccessDecisionEntry]
    next_cursor: str | None
    limit: int


@dataclass(frozen=True, slots=True)
class AccessDecisionQuery:
    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    actor_filter: UUID | None = None
    subject_type: str | None = None
    action: str | None = None
    result: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: str | None = None


def _validate_query(q: AccessDecisionQuery) -> None:
    """Validate the report's filter shape.

    The report endpoint enforces ``AUDIT_READ`` separately; this
    validator only checks the filter values are well-formed so the
    SQL adapter can be terse.
    """
    if q.limit < 1 or q.limit > MAX_PAGE_LIMIT:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_PAGE_LIMIT}",
            field="limit",
        )
    if q.since is not None and q.until is not None and q.since > q.until:
        raise ValidationFailed(
            "since must be <= until",
            field="since",
        )
    if q.result is not None and q.result not in VALID_RESULTS:
        raise ValidationFailed(
            f"unknown result filter: {q.result!r}",
            field="result",
        )


def _row_to_entry(row: Any) -> AccessDecisionEntry:
    """Project a persistence row onto the wire ``AccessDecisionEntry``.

    Why centralized: keeps the payload-allowlist and the wire shape
    in one place so a future "decision column" addition does not
    drift between routers and tests.
    """
    payload = dict(row.payload or {})
    actor_roles = payload.get(ACCESS_KEY_ACTOR_ROLES) or []
    return AccessDecisionEntry(
        id=row.id,
        occurred_at=row.occurred_at.isoformat(),
        actor_id=row.actor_id,
        actor_type=row.actor_type,
        actor_roles=list(actor_roles),
        action=payload.get(ACCESS_KEY_ACTION, ""),
        resource_kind=payload.get(ACCESS_KEY_RESOURCE_KIND, row.subject_type),
        resource_id=payload.get(ACCESS_KEY_RESOURCE_ID),
        result=payload.get(ACCESS_KEY_RESULT, ""),
        reason=payload.get(ACCESS_KEY_REASON, ""),
        correlation_id=row.correlation_id,
    )


async def view_report(
    query: AccessDecisionQuery,
    *,
    audit: AuditEventStore,
    cursor_encoder: Any | None = None,
) -> AccessDecisionPage:
    """Return a filtered page of access-decision rows for the report.

    The caller has already evaluated ``AUDIT_READ`` on the actor; this
    use case is the read-side counterpart to ``record_decision`` and
    never re-evaluates authorization. It only filters, paginates,
    and projects rows.

    Why a separate use case rather than reusing ``view_timeline``:
    the timeline is scoped to a single case (P09-US1). The access
    report is tenant-wide and filterable by actor, action, resource
    kind, result, and date range (P09-US6). Reusing the timeline
    would force a fake ``case_id`` and silently distort the WHERE
    clause.

    Filters are AND-ed; unspecified filters are no-ops. ``result``
    accepts only ``allowed`` or ``denied`` (validation rejects
    anything else as 422).
    """
    _validate_query(query)
    rows = await audit.list_access_decisions(
        tenant_id=query.tenant_id,
        actor_id=query.actor_filter,
        subject_type=query.subject_type,
        action=query.action,
        result=query.result,
        since=query.since,
        until=query.until,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [_row_to_entry(r) for r in rows]

    next_cursor: str | None = None
    if len(items) == query.limit and cursor_encoder is not None and rows:
        tail = rows[-1]
        next_cursor = cursor_encoder(tail.occurred_at, tail.id)

    return AccessDecisionPage(items=items, next_cursor=next_cursor, limit=query.limit)


__all__ = [
    "ACCESS_KEY_ACTION",
    "ACCESS_KEY_ACTOR_ROLES",
    "ACCESS_KEY_REASON",
    "ACCESS_KEY_RESOURCE_ID",
    "ACCESS_KEY_RESOURCE_KIND",
    "ACCESS_KEY_RESULT",
    "AccessDecisionEntry",
    "AccessDecisionInput",
    "AccessDecisionPage",
    "AccessDecisionQuery",
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "RESULT_ALLOWED",
    "RESULT_DENIED",
    "VALID_RESULTS",
    "build_recorder",
    "record_decision",
    "view_report",
]
