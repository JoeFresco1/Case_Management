"""Domain event catalog and minimal event envelope.

# INTEGRATION_SEAM: domain events

Domain events are the in-process, pure-Python representation of
"something happened that subscribers care about." They are emitted by
use cases after the domain transaction commits; the outbox dispatcher
turns them into rows in ``outbox`` and then into ``MessageBus``
publishes.

This module is the **single source of truth** for the event catalog
(P13-US8 / FM-055). Every event type the platform publishes is declared
here with its stable id, current schema version, and the minimal
PHI-free payload keys it may carry. Subscribers — internal and external —
fetch the catalog via ``GET /api/v1/events/catalog`` so they never need
to grep the source.

Why a separate module from ``audit``: domain events and audit events
have different lifecycles (P09 §1, AGENTS.md §16.1). Domain events
flow through the message bus and may have multiple consumers; audit
events are tamper-evident, per-tenant, and never deleted. They overlap
in *content* (a ``CaseCreated`` event is fanned out to both), not in
*contract*.

Versioning policy (P13 Q2, ADR-023): payloads are **additive-only**
within a major version. A breaking change (field removal, type change,
key rename) requires a new event-type id (e.g. ``CaseCreated.v2``).
The catalog stores both versions during the sunset window so dual
subscribers can drain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Catalog declarations
#
# An EventTypeSpec is the agreed contract between publisher and
# subscriber. The ``event_type`` string is the wire identifier — it
# never changes once published. ``version`` is the current schema
# version; subscribers that negotiate on version should compare against
# this. ``payload_keys`` is the whitelist of permitted keys — anything
# not in the list at publish time is rejected by ``validate_payload``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """The contract for a single event type in the catalog.

    Attributes:
        event_type: Wire identifier (stable, never changes).
        version: Current schema version (monotonic per ``event_type``).
        description: Human-readable summary used by ``/events/catalog``.
        payload_keys: Tuple of permitted payload keys (order-stable
            for predictable enumeration in the catalog response).
            Values are referenced by id (``case_id``, ``task_id``,
            ``document_id``); raw PHI is forbidden by construction.
    """

    event_type: str
    version: int
    description: str
    payload_keys: tuple[str, ...]


# First-slice catalog (FM-055). Each spec is the agreed contract;
# ``validate_payload`` rejects anything outside ``payload_keys``.
CASE_CREATED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="CaseCreated",
    version=1,
    description=(
        "A new case was created. Carries the case reference and the "
        "actor that initiated intake. Sensitive case data is referenced "
        "by id, not embedded."
    ),
    payload_keys=(
        "case_id",
        "reference",
        "case_type_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_COMPLETED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskCompleted",
    version=1,
    description=(
        "A worker task transitioned to completed. Carries task and "
        "case ids plus the completion outcome; task contents are not "
        "embedded."
    ),
    payload_keys=(
        "task_id",
        "case_id",
        "tenant_id",
        "actor_id",
        "outcome",
        "occurred_at",
    ),
)

DOCUMENT_ADDED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="DocumentAdded",
    version=1,
    description=(
        "A new document or document version was attached to a case. "
        "Carries the document id, sha256 (so a subscriber can dedupe "
        "without retrieving content), and classification."
    ),
    payload_keys=(
        "document_id",
        "case_id",
        "tenant_id",
        "actor_id",
        "sha256",
        "classification",
        "version_number",
        "occurred_at",
    ),
)

WORKFLOW_TRANSITIONED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="WorkflowTransitioned",
    version=1,
    description=(
        "A workflow instance moved between states. Carries the from / "
        "to state, the triggering transition key, and the workflow "
        "instance id; transition payload data is referenced by id."
    ),
    payload_keys=(
        "workflow_instance_id",
        "case_id",
        "tenant_id",
        "actor_id",
        "from_state",
        "to_state",
        "transition_key",
        "workflow_version",
        "occurred_at",
    ),
)

CASE_UPDATED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="CaseUpdated",
    version=1,
    description=(
        "A case was edited through the optimistic-concurrency path. "
        "Carries the case id, the new version token, and the actor; "
        "field contents are referenced by id, not embedded."
    ),
    payload_keys=(
        "case_id",
        "tenant_id",
        "actor_id",
        "version_token",
        "occurred_at",
    ),
)

TASK_CREATED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskCreated",
    version=1,
    description=(
        "A worker task was created (intake welcome task or transition "
        "follow-on). Carries task and case ids plus assignment; task "
        "contents are not embedded."
    ),
    payload_keys=(
        "task_id",
        "case_id",
        "tenant_id",
        "actor_id",
        "queue",
        "occurred_at",
    ),
)

DECISION_RECORDED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="DecisionRecorded",
    version=1,
    description=(
        "A case decision was recorded (Phase 4 lifecycle). Carries the "
        "decision and case ids plus the outcome; rationale text is "
        "referenced by id, not embedded."
    ),
    payload_keys=(
        "decision_id",
        "case_id",
        "tenant_id",
        "actor_id",
        "outcome",
        "occurred_at",
    ),
)

AUDIT_CORRECTION_RECORDED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="AuditCorrectionRecorded",
    version=1,
    description=(
        "A compensating correction row was appended for an audit event "
        "(audit history itself is never mutated). Carries both event "
        "ids so verifiers can walk the correction chain."
    ),
    payload_keys=(
        "event_id",
        "original_event_id",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)


# FM-016 queue + worker lifecycle (P02-US3, US9). The queue use cases
# enqueue these through the outbox in the same transaction as the
# queue/task change + audit row. Registering them here (FM-055) keeps
# the dispatcher from dead-lettering them as unknown types; payloads
# stay minimal (ids + keys, never task contents).
QUEUE_DEFINED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="QueueDefined",
    version=1,
    description=(
        "A queue was defined. Carries the queue id and stable key; "
        "membership is referenced by user id, never embedded."
    ),
    payload_keys=(
        "queue_id",
        "queue_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

QUEUE_RECONFIGURED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="QueueReconfigured",
    version=1,
    description=(
        "A queue was reconfigured (rename, policy, capacity, members). "
        "Carries the queue id and key; the new shape is read back "
        "through the queue admin API."
    ),
    payload_keys=(
        "queue_id",
        "queue_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

QUEUE_RETIRED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="QueueRetired",
    version=1,
    description=(
        "A queue was retired. Carries the queue id and key so "
        "subscribers can stop routing work to it."
    ),
    payload_keys=(
        "queue_id",
        "queue_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_CLAIMED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskClaimed",
    version=1,
    description=(
        "A worker claimed a task from a queue. Carries task and case "
        "ids plus the queue key; task contents are not embedded."
    ),
    payload_keys=(
        "task_id",
        "case_id",
        "queue_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_RELEASED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskReleased",
    version=1,
    description=(
        "A worker released a claimed task back to its queue. Carries "
        "task and case ids plus the queue key."
    ),
    payload_keys=(
        "task_id",
        "case_id",
        "queue_key",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_BULK_HANDLED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskBulkHandled",
    version=1,
    description=(
        "A worker bulk-claimed or bulk-released a batch of tasks. "
        "Carries counts, not the task bodies; per-task detail lives "
        "on each task's own event stream."
    ),
    payload_keys=(
        "action",
        "queue_key",
        "handled_count",
        "skipped_count",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_DELEGATED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskDelegated",
    version=1,
    description=(
        "A worker delegated open tasks to another identity for a "
        "bounded window. Carries the delegate id and the window end; "
        "the audit row carries the per-task list."
    ),
    payload_keys=(
        "delegate_user_id",
        "delegated_count",
        "until",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)

TASK_DELEGATION_EXPIRED: Final[EventTypeSpec] = EventTypeSpec(
    event_type="TaskDelegationExpired",
    version=1,
    description=(
        "A time-bounded delegation expired and the task was restored "
        "to its prior owner. Carries both identities so the timeline "
        "renders the full handoff arc without joining records."
    ),
    payload_keys=(
        "task_id",
        "case_id",
        "restored_user_id",
        "delegate_user_id",
        "tenant_id",
        "actor_id",
        "occurred_at",
    ),
)


# The ordered, exhaustive catalog. Order is preserved so the API
# response is stable across deployments (subscribers may iterate to
# build a registration manifest).
EVENT_CATALOG: Final[tuple[EventTypeSpec, ...]] = (
    CASE_CREATED,
    CASE_UPDATED,
    TASK_CREATED,
    TASK_COMPLETED,
    DOCUMENT_ADDED,
    WORKFLOW_TRANSITIONED,
    DECISION_RECORDED,
    AUDIT_CORRECTION_RECORDED,
    QUEUE_DEFINED,
    QUEUE_RECONFIGURED,
    QUEUE_RETIRED,
    TASK_CLAIMED,
    TASK_RELEASED,
    TASK_BULK_HANDLED,
    TASK_DELEGATED,
    TASK_DELEGATION_EXPIRED,
)


def find_event_spec(event_type: str, *, version: int | None = None) -> EventTypeSpec | None:
    """Return the catalog entry for ``event_type``, or ``None``.

    Why a public lookup: the outbox dispatcher, audit adapters, and
    the catalog endpoint all need to resolve ``event_type`` strings to
    the canonical spec. A linear scan is fine — the catalog is a
    tuple of fewer than ten entries by design (FM-047 will move this
    to a registered module map when tenant-defined types land).

    Args:
        event_type: Wire identifier (stable, never changes).
        version: When given, resolve that exact schema version;
            when ``None`` (default) resolve the highest version so
            publishers default to the current contract while
            subscribers can still pin an older version.

    Returns:
        The matching spec, or ``None`` when unknown.

    Failure mode:
        Never raises; unknown ``(event_type, version)`` returns ``None``
        so the dispatcher can dead-letter instead of publishing.
    """
    best: EventTypeSpec | None = None
    for spec in EVENT_CATALOG:
        if spec.event_type != event_type:
            continue
        if version is not None:
            if spec.version == version:
                return spec
            continue
        if best is None or spec.version > best.version:
            best = spec
    return best


# ---------------------------------------------------------------------------
# PHI guard
#
# Why a centralized redaction list instead of per-field guards in each
# use case: the catalog is small enough that a single list catches the
# vast majority of accidental leaks. Keys are matched case-insensitively
# and as substrings against the payload key set; a value-side redactor
# is intentionally NOT provided because events are minimal-payload by
# contract — if a value looks like PHI the right fix is to remove the
# field, not to redact its contents in the wire payload.
# ---------------------------------------------------------------------------


_PHI_KEY_NEEDLES: Final[tuple[str, ...]] = (
    "ssn",
    "dob",
    "birth",
    "address",
    "phone",
    "email",
    "passport",
    "license",
    "tax_id",
    "phi",
    "secret",
    "password",
    "token",
)


def _key_looks_like_phi(key: str) -> bool:
    needle = key.lower()
    return any(needle in n or n in needle for n in _PHI_KEY_NEEDLES)


def validate_payload(
    event_type: str, payload: dict[str, Any], *, version: int | None = None
) -> None:
    """Validate ``payload`` against the catalog spec for ``event_type``.

    Raises ``ValueError`` for:

    1. Unknown ``event_type`` (the catalog is the contract; a typo in
       a use case must fail loudly before the event is enqueued).
    2. Keys whose names suggest PHI (``ssn``, ``dob``, ``email`` …);
       these are forbidden by name regardless of the spec list. The
       redaction list runs **first** so a key that is both PHI-named
       and undeclared fails with the more informative message.
    3. Payload keys that are not in the spec whitelist (additive-only
       within a major version; new keys require a version bump).

    Args:
        event_type: Wire identifier to validate against.
        payload: Flat payload mapping to check.
        version: When given, validate against that schema version
            (a v1 subscriber pins v1 even after v2 publishes);
            when ``None`` validate against the current (highest) version.

    The validator does NOT deep-walk nested structures. Subscribers
    must not rely on nested data; the contract is flat payloads.
    """
    spec = find_event_spec(event_type, version=version)
    if spec is None:
        if version is None:
            raise ValueError(f"unknown event_type: {event_type!r}")
        raise ValueError(f"unknown event_type: {event_type!r} v{version}")
    # Phase 1: PHI guard runs against every key, regardless of the
    # whitelist. A key like ``patient_email`` is forbidden even if a
    # careless use-case author added it to the spec; the redaction
    # list is the safety net the spec whitelist cannot provide on its
    # own.
    for key in payload:
        if _key_looks_like_phi(key):
            raise ValueError(
                f"payload key {key!r} looks like PHI; events are "
                "minimal-payload, reference sensitive data by id"
            )
    # Phase 2: whitelist enforcement. Run after the PHI guard so the
    # more informative message wins for a key that is both PHI and
    # undeclared.
    allowed = set(spec.payload_keys)
    for key in payload:
        if key not in allowed:
            raise ValueError(
                f"payload key {key!r} not declared in catalog "
                f"for event {event_type!r} v{spec.version}"
            )
        if _key_looks_like_phi(key):
            raise ValueError(
                f"payload key {key!r} looks like PHI; events are "
                "minimal-payload, reference sensitive data by id"
            )


# ---------------------------------------------------------------------------
# Publication compatibility (FM-055 / FM-047)
#
# Why a pure checker instead of trusting the publisher: a breaking
# change (removed key, renamed key, type change modelled as removal +
# addition) silently breaks every subscriber pinned to the older
# version. The checker runs at publication/registration time so the
# break fails loudly with a named blocker instead of dead-lettering
# rows in production. Additive-only within an event_type: a higher
# version must carry every key of every lower version.
# ---------------------------------------------------------------------------


def is_backward_compatible(old: EventTypeSpec, new: EventTypeSpec) -> bool:
    """Return True when ``new`` can replace ``old`` without breaking subscribers.

    Args:
        old: The currently published spec (lower version).
        new: The candidate spec (higher version, same ``event_type``).

    Returns:
        True iff ``new`` keeps every ``old`` payload key (additive-only)
        and introduces no PHI-named keys. Version ordering and
        ``event_type`` equality are also required — a rename is a new
        type, not a new version (P13 Q2, ADR-023).

    Failure mode:
        Never raises; mismatched types or versions return False so the
        caller can raise the domain-appropriate error.
    """
    if old.event_type != new.event_type:
        return False
    if new.version <= old.version:
        return False
    old_keys = set(old.payload_keys)
    new_keys = set(new.payload_keys)
    if not old_keys.issubset(new_keys):
        return False
    return all(not _key_looks_like_phi(key) for key in new.payload_keys)


def validate_publication(
    existing: tuple[EventTypeSpec, ...],
    candidate: EventTypeSpec,
) -> None:
    """Validate ``candidate`` against ``existing`` specs before publishing.

    Raises ``ValueError`` for:

    1. A duplicate ``(event_type, version)`` with a different shape
       (same key, different keys/description) — re-publish the identical
       shape (idempotent retry) or bump the version.
    2. A breaking new version — same ``event_type`` at a higher version
       that drops a previously published key.
    3. A version regression — same ``event_type`` at a version that is
       not greater than the published max but carries a different shape.
    4. PHI-named payload keys — rejected by name at publication time so
       a careless module never reaches a subscriber.

    Args:
        existing: Already-published specs (the catalog slice for check).
        candidate: The spec proposed for publication.

    Failure mode:
        Raises ``ValueError`` with a named blocker; pure (no I/O).
    """
    for key in candidate.payload_keys:
        if _key_looks_like_phi(key):
            raise ValueError(
                f"payload key {key!r} looks like PHI; events are "
                "minimal-payload, reference sensitive data by id"
            )
    same_type = [s for s in existing if s.event_type == candidate.event_type]
    for spec in same_type:
        if spec.version == candidate.version and (
            set(spec.payload_keys) != set(candidate.payload_keys)
        ):
            raise ValueError(
                f"event {candidate.event_type!r} v{candidate.version} already published "
                "with a different shape; bump the version for a new shape"
            )
    if not same_type:
        return
    published_max = max(s.version for s in same_type)
    if candidate.version <= published_max:
        return  # older-or-same version pin; shape equality checked above
    for spec in same_type:
        if not set(spec.payload_keys).issubset(set(candidate.payload_keys)):
            removed = sorted(set(spec.payload_keys) - set(candidate.payload_keys))
            raise ValueError(
                f"event {candidate.event_type!r} v{candidate.version} is not "
                f"backward compatible with v{spec.version}; "
                f"removed keys: {removed}"
            )


# ---------------------------------------------------------------------------
# Envelope (unchanged public API from the foundation slice)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """The base shape every domain event implements.

    Subclasses are not required: most use cases build a ``DomainEvent``
    directly with the ``event_type`` set to the agreed string. The
    envelope guarantees that subscribers see a stable identifier
    (``event_id``), a tenant scope (``tenant_id``), a free-form
    JSON-safe ``payload``, and an ``occurred_at`` timestamp recorded at
    construction (default: ``Clock.now()`` via UTC; tests may override).
    """

    event_type: str
    tenant_id: UUID
    event_id: UUID = field(default_factory=uuid4)
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        # The catalog is the contract. We validate at construction so
        # a malformed event fails before it reaches the outbox — the
        # outbox is durable, so a bad row would replay forever.
        validate_payload(self.event_type, self.payload)

    def with_payload(self, **fields: Any) -> DomainEvent:
        """Return a copy with extra payload fields merged in.

        Why a helper: use cases commonly build a base payload
        (actor, subject) and then add subject-specific fields. Keeping
        the dataclass frozen forces the helper to build a new instance.
        The merged payload is re-validated against the catalog.
        """
        merged = dict(self.payload)
        merged.update(fields)
        return DomainEvent(
            event_type=self.event_type,
            tenant_id=self.tenant_id,
            event_id=self.event_id,
            payload=merged,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            occurred_at=self.occurred_at,
        )


__all__ = [
    "AUDIT_CORRECTION_RECORDED",
    "CASE_CREATED",
    "CASE_UPDATED",
    "DECISION_RECORDED",
    "DOCUMENT_ADDED",
    "DomainEvent",
    "EVENT_CATALOG",
    "EventTypeSpec",
    "QUEUE_DEFINED",
    "QUEUE_RECONFIGURED",
    "QUEUE_RETIRED",
    "TASK_BULK_HANDLED",
    "TASK_CLAIMED",
    "TASK_COMPLETED",
    "TASK_CREATED",
    "TASK_DELEGATED",
    "TASK_DELEGATION_EXPIRED",
    "TASK_RELEASED",
    "WORKFLOW_TRANSITIONED",
    "find_event_spec",
    "is_backward_compatible",
    "validate_payload",
    "validate_publication",
]
