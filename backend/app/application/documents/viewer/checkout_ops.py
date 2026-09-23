"""Checkout / checkin: a persistent exclusive lock with holder + expiry.

A conflicting acquire raises :class:`CheckoutConflict` carrying holder
+ expiry so the HTTP boundary renders a structured ``conflict``
envelope (T5 acceptance) — never a silent overwrite. The conflict type
is re-exported from this module (and from the package ``__init__``) so
callers can catch it without importing the domain package directly.

# Failure semantics

- No active row → acquire fresh (``DocumentCheckedOut``).
- Live row held by the requester → renew the lease in place
  (``DocumentCheckoutRenewed``; idempotent re-checkout).
- Live row held by another user → ``CheckoutConflict`` with holder +
  expiry, raised before any write.
- Expired row → acquire fresh (takeover mints a new row so the gap
  stays visible in checkout history).
- Checkin with no active row → ``NotFound``; a non-holder release
  raises ``CheckoutConflict`` with the true holder + expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.annotations import CheckoutConflict, CheckoutLock
from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    CheckoutLockRepository,
    DocumentRepository,
    OutboxWriter,
)

from .events import (
    DOCUMENT_CHECKED_IN,
    DOCUMENT_CHECKED_OUT,
    DOCUMENT_CHECKOUT_RENEWED,
)
from .helpers import _actor, _append_audit_and_outbox, _resolve_document


@dataclass(frozen=True, slots=True)
class CheckoutCommand:
    """Input to :func:`checkout_document`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    lease_seconds: int = 4 * 60 * 60


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Outcome of :func:`checkout_document` / :func:`checkin_document`."""

    lock: CheckoutLock
    renewed: bool


async def checkout_document(
    cmd: CheckoutCommand,
    *,
    document_repo: DocumentRepository,
    checkout_repo: CheckoutLockRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> CheckoutResult:
    """Acquire (or renew) the exclusive checkout on a document.

    - No active row → acquire fresh (``DocumentCheckedOut``).
    - Live row held by the requester → renew the lease in place
      (``DocumentCheckoutRenewed``; idempotent re-checkout).
    - Live row held by another user → ``CheckoutConflict`` carrying
      holder + expiry (structured ``conflict``, no silent overwrite).
    - Expired row → acquire fresh (takeover mints a new row so the
      gap stays visible in checkout history).
    """
    await authorize(
        Action.DOCUMENT_CHECKOUT,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    now = clock.now()
    active = await checkout_repo.get_active(cmd.tenant_id, cmd.document_id)
    if active is not None:
        # why: the pure rule decides — foreign live locks raise with
        # holder + expiry; same-holder and expired rows fall through
        # to renew / fresh-acquire below.
        active.check_acquire(requester_id=cmd.actor_id, now=now)
        if active.is_live(now=now):
            renewed = active.renew(
                requester_id=cmd.actor_id, now=now, lease_seconds=cmd.lease_seconds
            )
            await checkout_repo.save(renewed)
            await _append_audit_and_outbox(
                audit=audit,
                outbox=outbox,
                tenant_id=cmd.tenant_id,
                event_type=DOCUMENT_CHECKOUT_RENEWED,
                actor_id=cmd.actor_id,
                document_id=document.id,
                case_id=document.case_id,
                payload={
                    "case_id": str(document.case_id),
                    "document_id": str(document.id),
                    "checkout_id": str(renewed.id),
                    "expires_at": renewed.expires_at.isoformat(),
                },
                now=now,
            )
            return CheckoutResult(lock=renewed, renewed=True)
    fresh = CheckoutLock.acquire(
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        holder_user_id=cmd.actor_id,
        now=now,
        lease_seconds=cmd.lease_seconds,
    )
    await checkout_repo.add(fresh)
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_CHECKED_OUT,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "checkout_id": str(fresh.id),
            "holder": str(cmd.actor_id),
            "expires_at": fresh.expires_at.isoformat(),
        },
        now=now,
    )
    return CheckoutResult(lock=fresh, renewed=False)


@dataclass(frozen=True, slots=True)
class CheckinCommand:
    """Input to :func:`checkin_document` (release the checkout)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID


async def checkin_document(
    cmd: CheckinCommand,
    *,
    document_repo: DocumentRepository,
    checkout_repo: CheckoutLockRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> CheckoutResult:
    """Release the checkout held by the requester (``DocumentCheckedIn``).

    No active row → ``NotFound`` (nothing to release). A non-holder
    release raises ``CheckoutConflict`` with the true holder + expiry
    so the caller learns whom to ask instead of silently dropping a
    foreign lock.
    """
    await authorize(
        Action.DOCUMENT_CHECKOUT,
        _actor(tenant_id=cmd.tenant_id, user_id=cmd.actor_id, roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )
    document = await _resolve_document(
        tenant_id=cmd.tenant_id, document_id=cmd.document_id, document_repo=document_repo
    )
    now = clock.now()
    active = await checkout_repo.get_active(cmd.tenant_id, cmd.document_id)
    if active is None:
        raise NotFound(f"no active checkout for document {cmd.document_id}")
    # why: ``release`` enforces holder-only + double-release loudly;
    # a foreign attempt surfaces as CheckoutConflict (holder + expiry),
    # never as a silent no-op.
    released = active.release(requester_id=cmd.actor_id, now=now)
    await checkout_repo.save(released)
    await _append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_CHECKED_IN,
        actor_id=cmd.actor_id,
        document_id=document.id,
        case_id=document.case_id,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "checkout_id": str(released.id),
            "holder": str(cmd.actor_id),
        },
        now=now,
    )
    return CheckoutResult(lock=released, renewed=False)


__all__ = [
    "CheckinCommand",
    "CheckoutCommand",
    "CheckoutConflict",
    "CheckoutLock",
    "CheckoutLockRepository",
    "CheckoutResult",
    "checkin_document",
    "checkout_document",
]
