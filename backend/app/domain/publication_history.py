"""Publication-history domain (FM-021 / P10-US4, P10-US5, P10-US8).

This module is the **pure domain record** of configuration governance:
who authored what, who approved it, when it became effective, what
depends on it, and when it was retired. It covers every configuration
artifact kind (``CaseTypeVersion``, ``WorkflowVersion``, forms, queues)
through one shared vocabulary so the admin timeline UI (P10-US4) and
the impact view (002 T7) bind to a single contract.

# Why this lives in domain, not application

``app/application/configuration/governance.py`` orchestrates the same
lifecycle across aggregates and through Protocol ports (clock, repo,
policy) the domain does not own. This module owns the **invariants**
that need no I/O:

- published versions are immutable (FR-015, AC-007),
- retirement is ``published -> retired`` only (P10-US5),
- second-admin approval is separation-of-duties (P10-US8),
- effective-time resolution is a pure function over the entry list,
- the where-used / impact view is a pure function over an explicit
  caller-supplied dependent list (no new tables in this task — the
  dependents are read from existing rows: case bindings, workflow
  references, queue memberships).

# No new tables

Every function here takes plain ``Sequence[PublicationEntry]`` /
``Sequence[DependencyRef]`` inputs. Persistence reuses the existing
``configuration_publications`` table (``0006_create_configuration_and_
idempotency.py``); the repository stays append-only and tenant-scoped.
Callers (application use cases, routers) supply the rows; this module
never queries.

# Vocabulary

``PublicationEntry.action`` mirrors P10 §2 (``draft → validating →
published → retired``); the ``validate`` action doubles as the
second-admin approval row (P10-US8), matching the application layer.

INTEGRATION_SEAM: none — this module has no adapters. The durable
adapter is the existing ``SqlConfigurationPublicationRepository``;
a future Azure SQL adapter sits behind the same application Protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.errors import Forbidden, InvariantViolation, NotFound


class PublicationAction(StrEnum):
    """Lifecycle actions a publication entry can carry.

    The strings mirror P10 §2 and the ``configuration_publications``
    migration column comment. ``VALIDATE`` doubles as the second-admin
    approval row (P10-US8): an approval is a ``validate`` entry whose
    ``actor_id`` differs from the ``publish`` entry it follows.
    """

    DRAFT = "draft"
    VALIDATE = "validate"
    PUBLISH = "publish"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True)
class PublicationEntry:
    """One immutable publication-history row (FM-021 / P10-US4).

    Authorship is ``actor_id`` (who performed the action);
    effective time is ``occurred_at`` (when it took effect);
    the change summary is preserved verbatim so the admin timeline
    can render "what changed" without a join.
    """

    id: UUID
    tenant_id: UUID
    artifact_type: str
    artifact_id: UUID
    version_id: UUID
    action: PublicationAction
    actor_id: UUID
    occurred_at: datetime
    change_summary: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_type.strip():
            raise InvariantViolation("publication entry requires an artifact_type")


@dataclass(frozen=True, slots=True)
class DependencyRef:
    """One dependent of a configuration version (002 T7 impact view).

    ``kind`` names the dependent relationship (e.g.
    ``"case_binding"``, ``"workflow_reference"``, ``"queue_member"``);
    ``ref_id`` is the dependent row's id; ``detail`` is a short
    human-readable note (never PHI — a reference or key, not content).
    The list is supplied by the caller from existing rows; this module
    only filters and groups it, which is why no new table is needed.
    """

    version_id: UUID
    kind: str
    ref_id: UUID
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise InvariantViolation("dependency ref requires a kind")


# Version-row statuses (``CaseTypeVersionStatus`` / ``WorkflowVersionStatus``).
# Why literals instead of ``PublicationAction`` values: the status
# vocabulary (``published``) differs from the action vocabulary
# (``publish``), so sharing the enum would silently mismatch.
_STATUS_DRAFT = "draft"
_STATUS_PUBLISHED = "published"


def assert_version_mutable(status: str) -> None:
    """Raise unless ``status`` is ``draft``.

    Why a shared string guard alongside the entity methods: the
    repository layer works with status strings from the row, not with
    materialized entities. An UPDATE aimed at a row whose status is
    ``published`` (or ``retired``) is a bug and must raise
    ``InvariantViolation`` (FR-015, AC-007) instead of writing.
    Mirrors ``CaseTypeVersion.assert_mutable`` and
    ``WorkflowVersion.assert_mutable``.
    """
    if status != _STATUS_DRAFT:
        raise InvariantViolation(f"version is {status} and cannot be modified")


def assert_retire_allowed(status: str) -> None:
    """Raise unless ``status`` is ``published`` (P10-US5).

    Only a published version can be retired. A ``draft`` has nothing
    to retire (delete the draft instead); a ``retired`` version is
    already terminal — re-retiring would append a misleading row.
    Retirement never blocks on dependents: existing cases retain
    their binding to the retired version (FM-008), so the impact
    view (:func:`impact_view`) surfaces dependents without vetoing.
    """
    if status != _STATUS_PUBLISHED:
        raise InvariantViolation(f"only published versions can be retired (got {status})")


# ---------------------------------------------------------------------------
# Approval gate (P10-US8)
# ---------------------------------------------------------------------------


def latest_publish_for_version(
    entries: Sequence[PublicationEntry],
    version_id: UUID,
) -> PublicationEntry | None:
    """Return the newest ``publish`` entry for ``version_id``, if any.

    Why newest: a version is published at most once per lifecycle, but
    the entry list is append-only and caller-scoped, so the max by
    ``occurred_at`` is the deterministic pick under ties broken by id.
    """
    publishes = [
        e for e in entries if e.version_id == version_id and e.action is PublicationAction.PUBLISH
    ]
    if not publishes:
        return None
    publishes.sort(key=lambda e: (e.occurred_at, e.id))
    return publishes[-1]


def approve(
    entries: Sequence[PublicationEntry],
    *,
    tenant_id: UUID,
    artifact_type: str,
    artifact_id: UUID,
    version_id: UUID,
    approver_id: UUID,
    at: datetime,
    change_summary: str = "",
    entry_id: UUID | None = None,
) -> PublicationEntry:
    """Build the second-admin approval entry for a published version.

    Pure constructor — the caller appends the returned entry to the
    same store the ``publish`` row lives in. Raises ``NotFound``
    when no ``publish`` row exists for the version yet, and
    ``Forbidden`` when the approver is the original publisher
    (separation-of-duties: ``approval_self``).
    """
    publish = latest_publish_for_version(entries, version_id)
    if publish is None:
        raise NotFound(f"no pending publish for version {version_id}")
    if publish.actor_id == approver_id:
        raise Forbidden("approval_self: a different administrator must approve the publish")
    return PublicationEntry(
        id=entry_id if entry_id is not None else uuid4(),
        tenant_id=tenant_id,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        version_id=version_id,
        action=PublicationAction.VALIDATE,
        actor_id=approver_id,
        occurred_at=at,
        change_summary=change_summary,
    )


def assert_publish_approved(
    entries: Sequence[PublicationEntry],
    *,
    version_id: UUID,
    require_second_admin: bool,
) -> None:
    """Raise ``Forbidden`` when a required second approval is missing.

    When ``require_second_admin`` is off this is a no-op (single-admin
    publish is sufficient). When on, the newest ``publish`` row for
    the version must be followed by a ``validate`` row from a
    **different** actor. A missing row raises with
    ``approval_required``; a self-approved row raises with
    ``approval_self``. A version that was never published is not the
    gate's concern and passes silently (mirrors the application gate).
    """
    if not require_second_admin:
        return
    publish = latest_publish_for_version(entries, version_id)
    if publish is None:
        return
    validates = [
        e
        for e in entries
        if e.version_id == version_id
        and e.action is PublicationAction.VALIDATE
        and e.occurred_at >= publish.occurred_at
    ]
    if not validates:
        raise Forbidden("approval_required: a second administrator must approve this publish")
    validates.sort(key=lambda e: (e.occurred_at, e.id))
    if validates[-1].actor_id == publish.actor_id:
        raise Forbidden("approval_self: a different administrator must approve this publish")


# ---------------------------------------------------------------------------
# Effective time (P10-US4)
# ---------------------------------------------------------------------------


def effective_version_at(
    entries: Sequence[PublicationEntry],
    *,
    at: datetime,
) -> UUID | None:
    """Resolve which version was effective at time ``at``.

    A version becomes effective at its ``publish`` ``occurred_at`` and
    stops being effective at its ``retire`` ``occurred_at``. Among the
    versions effective at ``at``, the one with the latest publish time
    wins (linear history — branching is P10 P3). Returns ``None``
    when nothing was effective yet (e.g. only drafts exist).
    Entries dated after ``at`` are ignored so the answer is the
    historical truth for that moment, not the current truth.
    """
    published_at: dict[UUID, datetime] = {}
    retired: set[UUID] = set()
    for entry in entries:
        if entry.occurred_at > at:
            continue
        if entry.action is PublicationAction.PUBLISH:
            previous = published_at.get(entry.version_id)
            if previous is None or entry.occurred_at > previous:
                published_at[entry.version_id] = entry.occurred_at
        elif entry.action is PublicationAction.RETIRE:
            retired.add(entry.version_id)
    candidates = [
        (ts, version_id) for version_id, ts in published_at.items() if version_id not in retired
    ]
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


# ---------------------------------------------------------------------------
# Dependencies / impact view (002 T7, FM-021)
# ---------------------------------------------------------------------------


def where_used(
    version_id: UUID,
    dependents: Sequence[DependencyRef],
) -> tuple[DependencyRef, ...]:
    """Return every dependent bound to ``version_id``.

    The caller supplies ``dependents`` from existing rows (case
    version bindings, workflow references, queue memberships). This
    function only filters, so the impact view needs no new table.
    """
    return tuple(d for d in dependents if d.version_id == version_id)


def impact_view(
    version_id: UUID,
    dependents: Sequence[DependencyRef],
) -> dict[str, tuple[DependencyRef, ...]]:
    """Group :func:`where_used` results by ``kind`` for the admin UI.

    Keys are sorted so the rendered impact view is deterministic; refs
    within a kind sort by ``(ref_id, detail)``. An empty dict means
    "no known dependents" — retirement stays allowed either way
    because existing cases retain their binding (FM-008).
    """
    grouped: dict[str, list[DependencyRef]] = {}
    for dependent in where_used(version_id, dependents):
        grouped.setdefault(dependent.kind, []).append(dependent)
    return {
        kind: tuple(sorted(refs, key=lambda r: (str(r.ref_id), r.detail)))
        for kind, refs in sorted(grouped.items())
    }


__all__ = [
    "DependencyRef",
    "PublicationAction",
    "PublicationEntry",
    "approve",
    "assert_publish_approved",
    "assert_retire_allowed",
    "assert_version_mutable",
    "effective_version_at",
    "impact_view",
    "latest_publish_for_version",
    "where_used",
]
