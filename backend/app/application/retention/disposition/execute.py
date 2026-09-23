"""Execute an approved disposition with the hold-refusal seam (P08 / FM-039)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.retention.disposition.commands import (
    SUBJECT_TYPE_DISPOSITION_REQUEST,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)
from app.domain.audit import DISPOSITION_EXECUTED, DISPOSITION_REFUSED
from app.domain.errors import Conflict, NotFound
from app.domain.legal_hold import REFUSED_HOLD_CODE, refuse_disposition_under_hold
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DispositionRequestRepository,
    LegalHoldRepository,
    RetentionPolicyRepository,
)
from app.domain.retention import DispositionRequest, DispositionStatus

# ---------------------------------------------------------------------------
# Execute disposition (hold-refusal seam across case/document/audit)
# ---------------------------------------------------------------------------


class _DocumentLister(Protocol):
    """Minimal document surface the execution step needs.

    Why a local Protocol (not the full ``DocumentRepository``): the
    execution step only counts / verifies attached documents to prove
    "no partial delete" — it never mutates them. The duck-typed
    in-memory fake and the SQL adapter both satisfy this shape, and
    the use case stays decoupled from the full document port.
    """

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> Sequence[object]: ...


class _ExecuteIdempotencyStore(Protocol):
    """Minimal idempotency surface for execution replay.

    Why a local Protocol: the HTTP ``IdempotencyMiddleware`` already
    dedups at the edge, but the use case must also be safe when
    called twice with the same key (operator retry / outbox replay).
    The duck-typed ``InMemoryIdempotencyStore`` test fake satisfies
    this shape.
    """

    async def get(self, *, tenant_id: UUID, key: str) -> dict[str, object] | None: ...

    async def put(
        self,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, object],
        correlation_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecuteDispositionCommand:
    """Input to :func:`execute_disposition`.

    ``request_id`` is the approved :class:`DispositionRequest`
    surfaced by :func:`route_disposition_for_case`.
    ``idempotency_key`` is optional; when supplied the execution is
    replay-safe (a second call with the same key + same request id
    returns the stored outcome without a second audit row).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    request_id: UUID
    idempotency_key: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecuteDispositionResult:
    """The post-execution outcome returned to the router.

    ``outcome`` is ``"executed"`` when the hold-free, approved
    request ran to completion, or ``"refused_hold"`` (see
    :data:`app.domain.legal_hold.REFUSED_HOLD_CODE`) when an active
    hold blocked every scope. ``policy_version`` cites the published
    policy that surfaced the request so the evidence report can
    replay which horizon was evaluated. ``blocked_scopes`` is the
    closed set left untouched on refusal; ``document_count`` /
    ``audit_count`` are the evidence counts observed at execution
    time (they prove the scopes were inspected, not skipped).
    ``hold_id`` is the blocking hold on refusal (``None`` on success).
    """

    outcome: str
    request: DispositionRequest
    policy_id: UUID
    policy_version: int
    blocked_scopes: tuple[str, ...]
    document_count: int
    audit_count: int
    hold_id: UUID | None = None


async def execute_disposition(
    cmd: ExecuteDispositionCommand,
    *,
    disposition_repo: DispositionRequestRepository,
    policy_repo: RetentionPolicyRepository,
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
    document_repo: _DocumentLister | None = None,
    idempotency_store: _ExecuteIdempotencyStore | None = None,
) -> ExecuteDispositionResult:
    """Execute an approved disposition, refusing when a hold is active.

    The refusal seam (P08 / FM-039 × FM-040): a case under active
    legal hold blocks disposition of the case row, every attached
    document, and the audit evidence itself. The use case checks the
    hold *after* resolving the request + policy + case and *before*
    any delete so a held execution performs zero deletes (no partial
    delete by construction — v1 performs no physical deletes; the
    follow-up destruction dispatch lands behind the same transaction
    shape).

    Failure behavior:

    - Authorization (``DISPOSITION_APPROVE``) runs first; a missing
      grant surfaces as ``Forbidden`` before any DB read or write
      (fail-closed, Constitution V).
    - ``NotFound`` when the request, policy, or case is missing or
      cross-tenant. Tenant isolation is enforced by the
      tenant-scoped repository reads; the use case never confirms
      cross-tenant existence.
    - Held execution returns ``outcome="refused_hold"`` with the
      cited ``policy_version`` + the blocking ``hold_id`` +
      ``blocked_scopes=("case", "document", "audit")``. A
      ``DispositionRefused`` audit row lands in the same transaction
      as the (non-)execution so the refusal is auditable (audit
      atomicity). No disposition row is mutated and no rows are
      deleted.
    - Hold-free execution requires the request in ``approved``
      status; a ``pending`` / ``rejected`` row surfaces as
      ``Conflict`` (the approver must decide first). A
      ``DispositionExecuted`` audit row lands atomically.
    - Idempotent replay: when ``idempotency_key`` is supplied and the
      store holds a prior outcome for the same ``(tenant_id, key,
      request_id)``, the stored outcome is returned without a second
      audit row or second delete. A key bound to a different request
      id surfaces as ``Conflict``.

    Args:
        cmd: The execution command (tenant + actor + request id).
        disposition_repo: The disposition queue (tenant-scoped reads).
        policy_repo: The versioned policy that surfaced the request.
        case_repo: The case row (never deleted on refusal).
        hold_repo: The active-hold lookup (the refusal seam).
        audit: The append-only chain (refusal / execution rows).
        clock: The commit clock (shared by the audit row).
        document_repo: Optional document lister for evidence counts.
        idempotency_store: Optional replay store for operator retry.

    Returns:
        The execution outcome (``executed`` or ``refused_hold``).

    Raises:
        Forbidden: When the actor lacks ``DISPOSITION_APPROVE``.
        NotFound: When the request / policy / case is missing or
            cross-tenant.
        Conflict: When the hold-free request is not ``approved``, or
            when the idempotency key belongs to another request.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    # why: fail-closed — the hold check must never run for an
    # unauthorized actor, otherwise the existence of a hold leaks.
    await authorize(
        Action.DISPOSITION_APPROVE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(
            tenant_id=cmd.tenant_id,
            kind=SUBJECT_TYPE_DISPOSITION_REQUEST,
            id=cmd.request_id,
        ),
    )

    # why: idempotency pre-check runs before any read side-effect so a
    # retried operator call cannot double-append the refusal/execution
    # row. The hash binds the key to this request id; a key reused for
    # a different request is a client bug, not a replay.
    request_hash = str(cmd.request_id)
    if idempotency_store is not None and cmd.idempotency_key.strip():
        prior = await idempotency_store.get(
            tenant_id=cmd.tenant_id, key=cmd.idempotency_key.strip()
        )
        if prior is not None:
            if str(prior.get("request_hash", "")) != request_hash:
                raise Conflict("Idempotency-Key belongs to another disposition request")
            body = prior.get("response_body")
            if isinstance(body, dict):
                outcome = str(body.get("outcome", ""))
                if outcome in ("executed", REFUSED_HOLD_CODE):
                    live = await disposition_repo.get(cmd.tenant_id, cmd.request_id)
                    if live is None:
                        raise NotFound(f"disposition request {cmd.request_id} not found")
                    policy_row = await policy_repo.get(cmd.tenant_id, live.policy_id)
                    if policy_row is None:
                        raise NotFound(f"retention policy {live.policy_id} not found")
                    return ExecuteDispositionResult(
                        outcome=outcome,
                        request=live,
                        policy_id=policy_row.id,
                        policy_version=policy_row.version_number,
                        blocked_scopes=tuple(str(s) for s in body.get("blocked_scopes", ())),
                        document_count=int(body.get("document_count", 0)),
                        audit_count=int(body.get("audit_count", 0)),
                        hold_id=None,
                    )

    # why: tenant-scoped reads in dependency order — request first
    # (the resource the actor named), then the cited policy (the
    # version the refusal must cite), then the case (the row that must
    # survive a refusal). Each miss is NotFound so a cross-tenant id
    # is indistinguishable from a missing one.
    request = await disposition_repo.get(cmd.tenant_id, cmd.request_id)
    if request is None:
        raise NotFound(f"disposition request {cmd.request_id} not found")
    policy = await policy_repo.get(cmd.tenant_id, request.policy_id)
    if policy is None:
        raise NotFound(f"retention policy {request.policy_id} not found")
    case = await case_repo.get(cmd.tenant_id, request.case_id)
    if case is None:
        raise NotFound(f"case {request.case_id} not found")

    now = clock.now()
    document_count = await _count_documents(document_repo, cmd.tenant_id, case.id)
    audit_count = await _count_audit(audit, cmd.tenant_id, case.id)
    correlation_id = cmd.correlation_id or _new_correlation_id()

    # why: the hold lookup is the last read before the write — a hold
    # placed between route/approve and execute must still block. The
    # check covers the case row, every attached document, and the
    # audit evidence itself (the blocked_scopes triple); no scope is
    # deleted when the hold is active.
    hold = await hold_repo.get_active(cmd.tenant_id, case.id)
    if hold is not None and hold.is_active:
        refusal = refuse_disposition_under_hold(
            hold=hold,
            case_id=case.id,
            policy_id=policy.id,
            policy_version=policy.version_number,
        )
        # why: audit atomicity — the refusal row shares the caller's
        # transaction with the (non-)execution. No disposition row is
        # mutated and no case/document/audit rows are deleted, so a
        # rollback reverts exactly one row (the refusal itself).
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=DISPOSITION_REFUSED,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type=SUBJECT_TYPE_DISPOSITION_REQUEST,
            subject_id=request.id,
            case_id=case.id,
            correlation_id=correlation_id,
            causation_id=None,
            payload={
                **refusal.to_payload(),
                "case_reference": case.reference.value,
                "document_count": document_count,
                "audit_count": audit_count,
            },
            occurred_at=now,
        )
        result = ExecuteDispositionResult(
            outcome=refusal.code,
            request=request,
            policy_id=policy.id,
            policy_version=policy.version_number,
            blocked_scopes=refusal.blocked_scopes,
            document_count=document_count,
            audit_count=audit_count,
            hold_id=hold.id,
        )
        await _store_idempotency(
            idempotency_store,
            tenant_id=cmd.tenant_id,
            key=cmd.idempotency_key,
            request_hash=request_hash,
            result=result,
            correlation_id=correlation_id,
        )
        return result

    # why: fail-closed execution gate — only an approved request may
    # execute. A pending request means "not yet reviewed"; a rejected
    # request means "explicitly retained". Both refuse as Conflict so
    # the caller routes/reviews first instead of deleting silently.
    if request.status is not DispositionStatus.APPROVED:
        raise Conflict(
            f"disposition request {cmd.request_id} is {request.status.value}; "
            "only an approved request may execute"
        )

    # why: v1 performs no physical deletes — the execution row is the
    # durable proof that the hold-free, approved request ran. The
    # follow-up destruction dispatch will delete behind this same
    # transaction shape, so "no partial delete" holds by construction
    # today and by atomicity tomorrow.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSITION_EXECUTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSITION_REQUEST,
        subject_id=request.id,
        case_id=case.id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "case_reference": case.reference.value,
            "policy_id": str(policy.id),
            "policy_version": policy.version_number,
            "request_id": str(request.id),
            "document_count": document_count,
            "audit_count": audit_count,
        },
        occurred_at=now,
    )
    result = ExecuteDispositionResult(
        outcome="executed",
        request=request,
        policy_id=policy.id,
        policy_version=policy.version_number,
        blocked_scopes=(),
        document_count=document_count,
        audit_count=audit_count,
        hold_id=None,
    )
    await _store_idempotency(
        idempotency_store,
        tenant_id=cmd.tenant_id,
        key=cmd.idempotency_key,
        request_hash=request_hash,
        result=result,
        correlation_id=correlation_id,
    )
    return result


async def _count_documents(
    document_repo: _DocumentLister | None, tenant_id: UUID, case_id: UUID
) -> int:
    """Count attached documents for the execution evidence payload.

    The count proves the document scope was inspected, not skipped.
    A missing repository contributes zero (the scopes triple still
    cites ``document`` as blocked on refusal).
    """
    if document_repo is None:
        return 0
    rows = await document_repo.list_for_case(tenant_id=tenant_id, case_id=case_id)
    return len(rows)


async def _count_audit(audit: AuditEventStore, tenant_id: UUID, case_id: UUID) -> int:
    """Count case-scoped audit rows for the execution evidence payload.

    The count proves the audit-evidence scope was inspected. An
    adapter failure degrades to zero rather than failing the
    execution closed — the hold check itself never depends on the
    count.
    """
    try:
        rows = await audit.list_for_case(tenant_id=tenant_id, case_id=case_id, limit=200)
    except Exception:  # noqa: BLE001, S110 — evidence-only; never fail execution on count
        return 0
    try:
        return len(rows)
    except TypeError:
        return 0


async def _store_idempotency(
    store: _ExecuteIdempotencyStore | None,
    *,
    tenant_id: UUID,
    key: str,
    request_hash: str,
    result: ExecuteDispositionResult,
    correlation_id: str,
) -> None:
    """Persist the execution outcome for replay, when a key was supplied."""
    if store is None or not key.strip():
        return
    await store.put(
        tenant_id=tenant_id,
        key=key.strip(),
        request_hash=request_hash,
        response_status=200 if result.outcome == "executed" else 409,
        response_body={
            "outcome": result.outcome,
            "policy_version": result.policy_version,
            "blocked_scopes": list(result.blocked_scopes),
            "document_count": result.document_count,
            "audit_count": result.audit_count,
        },
        correlation_id=correlation_id,
    )
