"""Authorization policy registry.

The application layer is the only place that knows the **action vocabulary**
and the **role vocabulary**. This module provides a small, declarative
authorization model:

- `Action` — a controlled action name (e.g. ``case.create``).
- `Resource` — the object an action targets. Carries enough context
  (tenant_id, kind, id) for resource-aware rules.
- `Role` — a named bundle of `Action`s, defined declaratively.
- `Policy` — the registry. ``evaluate(action, user, resource) -> Decision``
  is **deny-by-default**: an action is allowed only if a published role
  binding explicitly allows it for the actor.

The policy is intentionally narrow for the first slice (FM-002). The full
action x role matrix, field-level redaction, separation-of-duties, and
break-glass all live in later slices (P11-US4..US12). What is wired here is
the **shape** the rest of the application will use.

Rules:

- Unknown action -> Deny (deny-by-default; see P11 pillar Q3).
- No matching role grant -> Deny.
- Cross-tenant actor (user.tenant_id != resource.tenant_id) -> Deny. This
  is defense-in-depth on top of explicit `WHERE tenant_id = :tenant_id`
  filters in every repository query; both must agree before access is
  allowed.
- ``is_system=True`` actors bypass role membership for any action that
  carries the ``SYSTEM_BYPASS`` flag. System actors are seeded internal
  jobs (outbox dispatcher, idempotency sweeper) that must operate across
  tenants. This is the only "bypass" path and it is explicit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from app.domain.context import UserContext
from app.domain.errors import Forbidden

if TYPE_CHECKING:
    from app.application.audit.access_audit import (
        AccessDecisionRecorder,
    )


# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------


class Action(StrEnum):
    """Controlled action names.

    New actions must be added here AND registered in at least one `Role`.
    """

    CASE_CREATE = "case.create"
    CASE_READ = "case.read"
    # P11-US6 / FM-029: read restricted/phi fields. Granted explicitly
    # to roles that need to see the value; default-deny.
    CASE_READ_SENSITIVE = "case.read_sensitive"
    CASE_UPDATE = "case.update"
    CASE_TRANSITION = "case.transition"
    # P02-US1 / FM-014 / FR-011: completing an assigned task. A
    # worker-role action that the workflow engine may pair with a
    # transition (the configured transition's guard fires after the
    # task completes). Distinct from ``case.transition`` because a
    # human-driven workflow can complete a task without firing a
    # transition (a notes-only task), and because the policy needs to
    # grant this independently for non-transitioning task types.
    TASK_COMPLETE = "task.complete"
    # P02-US3 / FM-014: a worker's task queue read. Listing and
    # viewing tasks a user can claim or work on. Distinct from
    # ``case.read`` so an operator can grant queue access without
    # granting case-wide visibility.
    TASK_READ = "task.read"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_READ = "document.read"
    # FM-025 / T5: strictly stronger read right for restricted documents
    # (sensitivity ``restricted`` / ``phi``) and for original
    # (pre-redaction) bytes. Never granted implicitly by
    # ``document.read``; a guessed-URL probe without this grant is
    # ``Forbidden`` with no metadata leak.
    DOCUMENT_READ_RESTRICTED = "document.read_restricted"
    # P03-US6 / US8: redaction + lock operations on a document.
    # Both require explicit grants because they are not in the
    # document.upload / document.read lineage.
    DOCUMENT_REDACT = "document.redact"
    DOCUMENT_LOCK = "document.lock"
    # T5 / FM-023 ext: viewer-pipeline actions. Annotation overlay CRUD,
    # persistent checkout/checkin, and the re-index request are
    # separately grantable from upload/read/redact so a tenant can give
    # reviewers markup rights without redaction rights. The annotation
    # path never mutates document bytes (``application.documents.viewer``).
    DOCUMENT_ANNOTATE = "document.annotate"
    DOCUMENT_CHECKOUT = "document.checkout"
    DOCUMENT_REINDEX = "document.reindex"
    CASE_TYPE_PUBLISH = "case_type.publish"
    WORKFLOW_PUBLISH = "workflow.publish"
    AUDIT_READ = "audit.read"
    # P09-US5 / FM-044: process administrators export audit evidence.
    # Distinct from AUDIT_READ because export leaves the system boundary
    # (downloadable artifact) and must be a separately grantable action.
    AUDIT_EXPORT = "audit.export"
    # P09-US7 / FM-044: process administrators append compensating
    # AuditCorrectionRecorded rows. Never an UPDATE path.
    AUDIT_CORRECT = "audit.correct"
    # P09-US8, US9 / FM-046: SIEM stream and signed-batch surface.
    # Reserved as **Gated** per ADR-009 — no role grants this action
    # today; the operator console flips the grant when the Azure
    # SIEM target is selected. Deny-by-default keeps the surface
    # off-limits until a product decision is made.
    AUDIT_STREAM = "audit.stream"
    ADMIN_TENANT_CONFIG = "admin.tenant_config"
    # P08-US1, US2 / FM-039: place or release a per-case legal hold.
    # The refuse-to-disposition guard on the Case aggregate (see
    # ``Case.change_status``) is the storage-layer enforcement; this
    # action is the authorization vocabulary. A security
    # administrator is the only role that should hold the hold
    # lifecycle — case workers cannot place holds even on cases
    # they own.
    CASE_HOLD_PLACE = "case.hold_place"
    CASE_HOLD_RELEASE = "case.hold_release"
    # P08-US3, US4 / FM-040: records administrator actions on the
    # retention policy lifecycle (publish, approve / reject a
    # disposition request). Granted only to the ``process_admin``
    # role — a case worker cannot publish a retention policy or
    # dispose of a case even when the record is past its horizon.
    # The refuse-to-dispose guard on the case aggregate is the
    # storage-layer enforcement (FM-039 H0 seam); this action is
    # the authorization vocabulary.
    RETENTION_POLICY_PUBLISH = "retention.policy_publish"
    DISPOSITION_APPROVE = "retention.disposition_approve"
    DISPOSITION_REJECT = "retention.disposition_reject"
    # P08-US5, US7, US8 / FM-041: archival tier move + unfreeze,
    # disposal-freeze declaration / cancellation, and orphan
    # proposal surfacing / dismissal. The records administrator is
    # the only role that should hold these actions — case workers
    # cannot archive their own cases or surface orphans even when
    # the case is past its retention horizon. The freeze and
    # archival actions are explicitly security-administrator
    # actions because they shape the platform's defensible-
    # disposition posture for the entire tenant.
    RETENTION_ARCHIVE = "retention.archive"
    RETENTION_UNFREEZE = "retention.unfreeze"
    RETENTION_FREEZE_DECLARE = "retention.freeze_declare"
    RETENTION_FREEZE_CANCEL = "retention.freeze_cancel"
    RETENTION_ORPHAN_SURFACE = "retention.orphan_surface"
    RETENTION_ORPHAN_DISMISS = "retention.orphan_dismiss"
    # P11-US11 / FM-049: publish the set of privileged actions that
    # require an approved client certificate (privileged-access-workstation
    # policy). Reserved as **Gated** per ADR-011 — no role grants this
    # action today; the operator console flips the grant when the
    # production target is selected. Deny-by-default keeps the surface
    # off-limits until a product decision is made.
    SESSION_REQUIRE_CERT = "session.require_cert"
    # P11-US8 / FM-049: publish the session-control policy (idle /
    # absolute timeouts, failed-attempt threshold, lockout window,
    # privileged-action set). Reserved as **Gated** per ADR-011 for
    # the same reason as ``SESSION_REQUIRE_CERT``: the surface is the
    # tenant-wide lockout lever and is deny-by-default until a
    # product decision is made.
    SESSION_POLICY_PUBLISH = "session.policy_publish"
    # P14-US5 / FM-058: implement a **named** enterprise connector
    # (any kind other than ``synthetic``). Reserved as **Gated** per
    # Constitution V.10 / XII.5 / ADR-015 — no role grants this
    # action today. The synthetic connector can be approved by
    # ``ADMIN_TENANT_CONFIG``; a named connector requires this action
    # *additionally* and is otherwise denied-by-default so a misconfigured
    # role grant cannot silently enable enterprise integration.
    CONNECTOR_NAMED_IMPLEMENT = "connector.named_implement"


# Actions that an internal ``is_system`` actor may perform regardless of
# role membership. This is the only path that bypasses the role registry and
# it is explicit; every such action must be reviewed alongside the system
# job it serves (outbox dispatcher, idempotency sweeper, hash-chain
# verifier).
SYSTEM_BYPASS: Final[frozenset[Action]] = frozenset()


# ---------------------------------------------------------------------------
# Resource and decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resource:
    """A resource the policy is asked about.

    Every protected resource carries its ``tenant_id`` so the policy can
    refuse cross-tenant access even if a misconfigured upstream forgot to
    filter.
    """

    tenant_id: UUID
    kind: str  # 'case' | 'document' | 'workflow' | 'case_type' | ...
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str

    @classmethod
    def allow(cls, reason: str = "allowed") -> Decision:
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> Decision:
        return cls(allowed=False, reason=reason)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Role:
    """A named bundle of actions.

    A role grants every action in ``actions`` to anyone whose ``UserContext``
    carries it. The role name is the IdP claim value (local seed strings
    like ``"intake"`` or ``"process_admin"``).
    """

    name: str
    actions: frozenset[Action]


# First-slice role catalog. Mirrors the actions actually wired in the
# walking skeleton; extended in FM-048 (Role and permission administration).
ROLE_INTAKE: Final[Role] = Role(
    name="intake",
    actions=frozenset({Action.CASE_CREATE, Action.CASE_READ}),
)
ROLE_REVIEWER: Final[Role] = Role(
    name="reviewer",
    actions=frozenset(
        {
            Action.CASE_READ,
            Action.CASE_UPDATE,
            Action.CASE_TRANSITION,
            Action.TASK_READ,
            Action.TASK_COMPLETE,
            Action.DOCUMENT_UPLOAD,
            Action.DOCUMENT_READ,
            Action.DOCUMENT_ANNOTATE,
            Action.DOCUMENT_CHECKOUT,
            Action.DOCUMENT_REINDEX,
        }
    ),
)
ROLE_SUPERVISOR: Final[Role] = Role(
    name="supervisor",
    actions=frozenset(
        {
            Action.CASE_READ,
            Action.CASE_READ_SENSITIVE,
            Action.CASE_UPDATE,
            Action.CASE_TRANSITION,
            Action.TASK_READ,
            Action.TASK_COMPLETE,
            Action.DOCUMENT_READ,
            Action.DOCUMENT_READ_RESTRICTED,
            Action.DOCUMENT_REDACT,
            Action.DOCUMENT_LOCK,
            Action.DOCUMENT_ANNOTATE,
            Action.DOCUMENT_CHECKOUT,
            Action.DOCUMENT_REINDEX,
            Action.AUDIT_READ,
        }
    ),
)
ROLE_PROCESS_ADMIN: Final[Role] = Role(
    name="process_admin",
    actions=frozenset(
        {
            Action.CASE_CREATE,
            Action.CASE_READ,
            Action.CASE_READ_SENSITIVE,
            Action.CASE_UPDATE,
            Action.CASE_TRANSITION,
            Action.DOCUMENT_UPLOAD,
            Action.DOCUMENT_READ,
            Action.DOCUMENT_READ_RESTRICTED,
            Action.TASK_READ,
            Action.TASK_COMPLETE,
            Action.DOCUMENT_REDACT,
            Action.DOCUMENT_LOCK,
            Action.DOCUMENT_ANNOTATE,
            Action.DOCUMENT_CHECKOUT,
            Action.DOCUMENT_REINDEX,
            Action.CASE_TYPE_PUBLISH,
            Action.WORKFLOW_PUBLISH,
            Action.AUDIT_READ,
            Action.AUDIT_EXPORT,
            Action.AUDIT_CORRECT,
            Action.ADMIN_TENANT_CONFIG,
            Action.CASE_HOLD_PLACE,
            Action.CASE_HOLD_RELEASE,
            Action.RETENTION_POLICY_PUBLISH,
            Action.DISPOSITION_APPROVE,
            Action.DISPOSITION_REJECT,
            Action.RETENTION_ARCHIVE,
            Action.RETENTION_UNFREEZE,
            Action.RETENTION_FREEZE_DECLARE,
            Action.RETENTION_FREEZE_CANCEL,
            Action.RETENTION_ORPHAN_SURFACE,
            Action.RETENTION_ORPHAN_DISMISS,
        }
    ),
)


DEFAULT_ROLES: Final[tuple[Role, ...]] = (
    ROLE_INTAKE,
    ROLE_REVIEWER,
    ROLE_SUPERVISOR,
    ROLE_PROCESS_ADMIN,
)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class Policy:
    """Action -> Allow/Deny registry.

    Use cases call ``policy.evaluate(action, user, resource)`` and either
    trust the decision or, on deny, translate it to a `Forbidden` domain
    error. The policy never raises on its own; the caller decides how to
    surface denial (HTTP 403, CLI error, batch failure).

    # Live role permissions (P11-US4)

    When constructed with ``role_repository``, ``evaluate`` joins the
    actor's ``user.roles`` (the JSON/IdP claim string names) against
    the static ``Role`` registry AND the live ``roles`` table. The two
    sources are unioned, so granting a permission via
    ``update_role_permissions`` takes effect on the very next request.

    Note on shape: ``user.roles`` is the actor's *role-name* set (the
    claim), not the actor's permission set. The Policy looks up each
    role's permission set, then tests the requested action. A user with
    ``("intake",)`` in their context gains exactly the ``Action`` set
    defined for the role named ``intake`` — whatever the registry or
    the DB row currently says.
    """

    def __init__(
        self,
        roles: Iterable[Role] = DEFAULT_ROLES,
        role_repository: object | None = None,
    ) -> None:
        # Why: build the lookup once; mutations to the registry at runtime
        # would silently break authorization.
        self._role_index: dict[str, Role] = {r.name: r for r in roles}
        self._role_actions: dict[str, frozenset[Action]] = {r.name: r.actions for r in roles}
        # The role repository is optional. When supplied, ``evaluate``
        # looks up the live permission set for each role name on every
        # call so revocation / addition takes effect on the next
        # request without any cache invalidation.
        self._role_repository = role_repository

    def role_actions(self, role_name: str) -> frozenset[Action]:
        return self._role_actions.get(role_name, frozenset())

    def roles(self) -> tuple[str, ...]:
        return tuple(self._role_index)

    async def _live_role_actions(
        self, tenant_id: UUID, user_id: UUID, role_names: tuple[str, ...]
    ) -> frozenset[Action]:
        """Look up the union of actions the actor actually holds.

        A user with role names in their IdP claim is granted the union
        of permissions for each role they are **assigned to** in the
        live ``role_assignments`` table. The semantics match the use
        case: a user who only carries the name in their claim but has
        no assignment is not granted (this is the seam between identity
        and authorization; the claim alone is informational).

        Unknown role names and stale permission strings are silently
        ignored so a malformed persisted row cannot crash the policy.
        """
        repo = self._role_repository
        if repo is None:
            return frozenset()
        # Duck-typed: permissions_for_user returns the union of
        # ``Action.value`` strings for the user's assigned roles.
        perms = await repo.permissions_for_user(tenant_id, user_id)  # type: ignore[attr-defined]
        out: set[Action] = set()
        for perm in perms:
            try:
                out.add(Action(perm))
            except ValueError:
                continue
        # ``role_names`` is unused in the live lookup (it is the user's
        # IdP claim; the authoritative list of roles they hold is the
        # assignment table). We accept it to keep the signature
        # symmetric with the static role check above.
        _ = role_names
        return frozenset(out)

    async def evaluate(
        self,
        action: Action,
        user: UserContext,
        resource: Resource,
    ) -> Decision:
        """Evaluate the policy.

        Order matters:

        1. Cross-tenant actors are denied unconditionally.
        2. System actors bypass role checks for actions in
           ``SYSTEM_BYPASS`` (currently empty; reserved for internal jobs).
        3. The actor's roles are checked against the static registry;
           a single matching role grants the action.
        4. When a ``role_repository`` is configured, the actor's roles
           are also looked up live; the union of permissions is used.
        5. Unknown (non-enum) action -> deny (defensive default).
        """
        if not isinstance(action, Action):
            return Decision.deny("unknown action")

        # 1. Cross-tenant isolation. The repository must also filter by
        # tenant_id; this is belt-and-suspenders (Constitution V).
        if user.tenant_id != resource.tenant_id:
            return Decision.deny("cross-tenant access")

        # 2. System actors. Bypass is explicit and scoped.
        if user.is_system and action in SYSTEM_BYPASS:
            return Decision.allow("system bypass")

        # 3. Static role membership.
        for role_name in user.roles:
            if action in self.role_actions(role_name):
                return Decision.allow(f"role:{role_name}")

        # 4. Live role lookup. The actor's assignment table is the
        # authoritative source for what roles they currently hold. If
        # no role repository is wired this is a no-op and the static
        # registry has already had its say.
        if self._role_repository is not None:
            live = await self._live_role_actions(user.tenant_id, user.user_id, user.roles)
            if action in live:
                return Decision.allow("role-assignment")

        # 5. No matching grant.
        return Decision.deny("no role grants this action")


# Default policy used by the application. Swap for a published registry
# when FM-048 lands.
_default_policy: Policy | None = None


def default_policy() -> Policy:
    global _default_policy  # noqa: PLW0603
    if _default_policy is None:
        _default_policy = Policy()
    return _default_policy


def configure_default_policy(policy: Policy) -> None:
    """Replace the process-wide default policy.

    Called by ``app.deps`` after the role repository is wired so the
    policy can read live assignments. Tests that need a hermetic policy
    can call this with a freshly constructed ``Policy`` to reset state.
    """
    global _default_policy  # noqa: PLW0603
    _default_policy = policy


async def authorize(
    action: Action,
    user: UserContext,
    resource: Resource,
    *,
    policy: Policy | None = None,
    role_repository: object | None = None,
    access_recorder: AccessDecisionRecorder | None = None,
    correlation_id: str = "",
) -> None:
    """Raise `Forbidden` if the policy denies `action`.

    Why a thin wrapper: every use case should call exactly one function to
    enforce authorization. Returning ``Decision`` from each call site is
    easy to forget; a raising helper makes omission a runtime error.

    Why ``async``: ``Policy.evaluate`` reads the live role-assignment
    store (P11-US4) so revocation takes effect on the next request.

    Why ``role_repository``: callers (HTTP routers) may supply the
    per-request ``RoleRepository`` so the live ``permissions_for_user``
    lookup shares the request's session and transaction. Without an
    explicit repository, ``authorize`` falls back to the static registry.

    Why ``access_recorder``: P09-US6 / P11-US9 require every
    authorization decision (allow and deny) to land on the audit
    chain. The recorder is optional; when supplied, every call writes
    one ``AccessDecisionRecorded`` row in the same transaction as
    the calling use case so the audit chain captures what was
    decided, by whom, against which resource, and why. When the
    recorder is ``None`` the function still authorizes correctly —
    unit tests and the CLI path do not need the audit row.
    """
    if policy is None:
        if role_repository is not None:
            policy = Policy(role_repository=role_repository)
        else:
            policy = default_policy()
    decision = await policy.evaluate(action, user, resource)
    if access_recorder is not None:
        await access_recorder(action, user, resource, decision, correlation_id)
    if not decision.allowed:
        raise Forbidden(f"{action.value} denied: {decision.reason}")


__all__ = [
    "Action",
    "Decision",
    "DEFAULT_ROLES",
    "Policy",
    "Resource",
    "ROLE_INTAKE",
    "ROLE_REVIEWER",
    "ROLE_SUPERVISOR",
    "ROLE_PROCESS_ADMIN",
    "SYSTEM_BYPASS",
    "authorize",
    "default_policy",
]
