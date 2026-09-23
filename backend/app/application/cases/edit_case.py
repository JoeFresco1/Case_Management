"""Edit a case atomically with optimistic concurrency (P01-US3, FM-007, FR-003).

The use case is the canonical atomic case-edit flow:

1. **Authorize** the actor against ``Action.CASE_UPDATE`` on the target
   case (P11-US2 / FM-002). Cross-tenant actors are denied
   unconditionally — the repository never matches a case from another
   tenant so the policy is belt-and-suspenders on the actor's own
   tenant_id.
2. **Resolve the case**, tenant-scoped. A missing or cross-tenant id
   surfaces as ``NotFound``; the two are intentionally
   indistinguishable so the API never confirms cross-tenant existence
   (Constitution V).
3. **Apply the PATCH** via :meth:`Case.edit` with the caller-supplied
   ``expected_version_token`` (the ``If-Match`` header at the HTTP
   boundary). The aggregate refuses the edit and raises
   :class:`Conflict` when its current token differs — never silently
   overwriting a peer's write (P01-US3 acceptance scenario 3).
4. **Persist case + audit + outbox** in the same DB transaction:
   ``cases`` UPDATE gated by the version token, ``audit_events`` with
   the field-level diff (P01-US3 "field-level history preserved"),
   ``outbox`` with a ``CaseUpdated`` event. No partial commit; the
   boundary commits once.

# INTEGRATION_SEAM: HTTP entry

``PATCH /api/v1/cases/{id}`` is the canonical entry point. The
``If-Match: <version_token>`` header is **required** (AGENTS.md §10);
the router maps a missing header to 400 and a stale token to 409 with
the current token in the error body so the client can refetch.

Idempotent replay: the HTTP ``Idempotency-Key`` middleware (FR-001 /
AGENTS.md §17.1) is the layer that handles transport-level replays;
this use case is naive about keys. A second call with the same body
and a current token still produces a new audit row (a new write is a
new write), so clients retrying after a network failure must reuse the
``Idempotency-Key`` to land on the cached response.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.cases.compute_derived import compute_derived_fields
from app.domain.audit import CASE_UPDATED
from app.domain.case import Case, FieldDiff, FieldSnapshot
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    OutboxWriter,
)


class _ClockLike(Protocol):
    """Minimal clock protocol used by :func:`handle`.

    Why a local Protocol rather than importing ``app.deps.Clock``: the
    application layer must not depend on FastAPI / ``app.deps``. The
    duck-typed ``FixedClock`` test fake (and any future adapter)
    satisfy this shape.
    """

    def now(self) -> datetime: ...


SUBJECT_TYPE_CASE = "case"


@dataclass(frozen=True, slots=True)
class EditCaseCommand:
    """Input to :func:`handle`.

    All fields except ``tenant_id``, ``actor_id``, ``actor_roles``,
    ``case_id``, and ``expected_version_token`` are optional — only the
    fields the caller actually intends to change are present in the
    PATCH body (P01-US3). The use case threads ``None`` through to
    :meth:`Case.edit`, which leaves absent fields alone.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    expected_version_token: int
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    data: dict[str, Any] | None = None
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class EditResult:
    """The post-commit outcome returned to the router.

    ``case`` is the updated aggregate so the router can build a
    fresh ``CaseSummary``. ``diff`` is the before/after field-level
    history (P01-US3 "field-level history preserved") — the router
    surfaces the new ``version_token`` in the response so the next
    ``If-Match`` is correct.
    """

    case: Case
    diff: FieldDiff


async def handle(
    cmd: EditCaseCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
) -> EditResult:
    """Apply a PATCH to a case atomically with audit + outbox.

    Failure behavior:
    - Auth/validation/not-found errors are raised before any DB write.
    - ``Conflict`` (stale ``version_token``) is raised by the
      aggregate **before** any audit or outbox row is staged, so a
      rejected edit never leaves provenance noise behind.
    - The case UPDATE, audit row, and outbox row share one transaction;
      the caller commits once. If the audit or outbox write fails the
      case UPDATE is rolled back.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_UPDATE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # Resolve the case type version up front so we can validate ``data``
    # edits against the published schema (FR-003). The aggregate checks
    # the id matches; the schema validation runs inside
    # ``Case.edit``. The version is the same one pinned at creation.
    case_type_version = await case_type_version_repo.get(cmd.tenant_id, case.case_type_version_id)
    if case_type_version is None:
        # The pinned version was retired or otherwise unpublished; the
        # case remains bound to it for reads (Constitution XIV — no
        # silent migrations), but edits cannot validate against a
        # missing schema. Surface this as not_found so the operator
        # knows the case needs re-binding.
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    now = clock.now()
    new_case = case.edit(
        expected_version_token=cmd.expected_version_token,
        title=cmd.title,
        description=cmd.description,
        priority=cmd.priority,
        data=cmd.data,
        case_type_version=case_type_version if cmd.data is not None else None,
        now=now,
    )

    # The aggregate always bumps the token on a real edit; a no-op
    # edit returns the original case. Skip audit + outbox for no-ops so
    # the audit chain stays clean.
    if new_case.version_token == case.version_token:
        return EditResult(case=new_case, diff=FieldDiff())

    # FM-012 / P05-US6: when ``data`` is being edited, re-derive the
    # derived fields so the stored snapshot is the contract.
    if cmd.data is not None:
        final_data = compute_derived_fields(dict(new_case.data), case_type_version.schema)
        if final_data != new_case.data:
            new_case = replace(new_case, data=final_data)

    # Recompute the diff against the *stored* (derived) snapshot so the
    # audit payload matches what the database will hold.
    diff = FieldDiff(
        before=_snapshot_of(case),
        after=_snapshot_of(new_case),
        changed_fields=_changed_fields(case, new_case),
    )

    await case_repo.update(new_case)

    # why (FM-011 / Constitution V): the audit payload is durable and
    # leaves the process through the timeline/export surfaces. The
    # diff's before/after ``data`` blobs may carry restricted/phi
    # values, so redact them by reference id before appending. The
    # timeline allowlist would mask the whole ``field_diff`` key on
    # read, but the stored row must already carry refs, never content.
    audit_payload = {
        "reference": new_case.reference.value,
        "version_token": new_case.version_token,
        "previous_version_token": case.version_token,
        "field_diff": _redacted_diff_payload(diff, case_type_version.schema),
    }
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=CASE_UPDATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=new_case.id,
        case_id=new_case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=cmd.causation_id,
        payload=audit_payload,
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=CASE_UPDATED,
        event_id=uuid4(),
        payload={
            "case_id": str(new_case.id),
            "reference": new_case.reference.value,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "version_token": new_case.version_token,
            "previous_version_token": case.version_token,
            "changed_fields": sorted(diff.changed_fields),
            "occurred_at": now.isoformat(),
        },
    )

    return EditResult(case=new_case, diff=diff)


# ----- helpers --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _snapshot_of(case: Case) -> FieldSnapshot:
    """Capture the editable fields of a case for the audit diff."""
    return FieldSnapshot(
        title=case.title,
        description=case.description,
        priority=case.priority,
        data=dict(case.data),
    )


def _changed_fields(before: Case, after: Case) -> frozenset[str]:
    """Return the set of editable fields that differ between two cases."""
    changed: set[str] = set()
    if before.title != after.title:
        changed.add("title")
    if before.description != after.description:
        changed.add("description")
    if before.priority != after.priority:
        changed.add("priority")
    if before.data != after.data:
        changed.add("data")
    return frozenset(changed)


def _redacted_diff_payload(diff: FieldDiff, schema: Any) -> dict[str, Any]:
    """Render the audit diff with sensitive ``data`` values redacted by ref.

    Args:
        diff: the before/after field-level history for the edit.
        schema: the published ``CaseTypeSchema`` declaring field
            sensitivity. Only ``sensitive_field_keys()`` is read;
            values are never inspected to decide sensitivity.

    Returns:
        The ``FieldDiff.to_payload()`` shape with every restricted/phi
        ``data`` entry replaced by
        ``{"redacted": True, "reason": "sensitive", "ref": <key>}``.
        Non-sensitive entries pass through verbatim. ``changed_fields``
        (key names only) is preserved so forensics keep the key list
        without the values.
    """
    raw = diff.to_payload()
    try:
        sensitive = set(schema.sensitive_field_keys())
    except AttributeError:
        return raw
    if not sensitive:
        return raw
    redacted = dict(raw)
    for section in ("before", "after"):
        snapshot = raw.get(section)
        if not isinstance(snapshot, dict):
            continue
        snapshot_copy = dict(snapshot)
        data = snapshot.get("data")
        if isinstance(data, dict):
            snapshot_copy["data"] = {
                key: (
                    {"redacted": True, "reason": "sensitive", "ref": key}
                    if key in sensitive
                    else value
                )
                for key, value in data.items()
            }
        redacted[section] = snapshot_copy
    return redacted


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "EditCaseCommand",
    "EditResult",
    "handle",
]
