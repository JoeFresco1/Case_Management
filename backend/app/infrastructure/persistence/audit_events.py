"""ORM-level immutability listener for ``AuditEvent``.

This is **layer 2** of the four-layer append-only enforcement (AGENTS.md
§4.R12, plan §4.R12):

1. **Database trigger** — installed by alembic migration ``0002``.
2. **ORM listener** (this module) — raises before SQLAlchemy flushes
   an UPDATE or DELETE.
3. **Application** — no code path that issues UPDATE/DELETE.
4. **Migrations** — forward-only; corrections are compensating events.

# INTEGRATION_SEAM: audit

Why a separate module: the listener is wired from ``app/db.py`` at
engine-construction time. Keeping it isolated means the dependency
direction (infrastructure -> domain) is preserved and the listener is
tested independently of the engine lifecycle.

Why raise on ``before_update`` / ``before_delete``: SQLAlchemy calls
the listener before the row is flushed. By raising ``AuditEventImmutable``
we stop the flush *and* the session enters a failed state; the
transaction is rolled back without ever issuing the UPDATE/DELETE
statement. The behavior matches the DB trigger for clients that go
through the ORM.

What we deliberately do not do: silently log. The whole point of the
listener is to make mutation *loud* — a successful UPDATE or DELETE
would indicate a security bug, not an optimization opportunity.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy import delete, event, update
from sqlalchemy.orm import Mapper

from app.infrastructure.persistence.models import AuditEvent as AuditEventRow


class AuditEventImmutable(Exception):  # noqa: N818
    """Raised when application code attempts to UPDATE or DELETE an AuditEvent.

    Why not the conventional ``Error`` suffix: the exception class is
    internal to the audit subsystem and is exported as the public
    contract for the immutability guarantee (the DB trigger error
    code matches). Renaming to ``AuditEventImmutableError`` would
    create a parallel name and increase review surface for no
    behavioral gain. The DB trigger's error code is the authoritative
    contract; this class is the matching Python-side exception.

    The error code (``AUDIT_EVENTS_APPEND_ONLY``) matches the trigger's
    error code so client code can detect the class of failure without
    parsing message strings.
    """

    CODE = "AUDIT_EVENTS_APPEND_ONLY"

    def __init__(self, operation: str) -> None:
        super().__init__(f"{self.CODE}: audit_events are append-only ({operation} refused)")
        self.operation = operation


_LISTENER_INSTALLED = False  # noqa: PLW0603  (module-level mutable state by design)


def _on_before_update(_mapper: Mapper, _connection: object, _target: object) -> None:
    raise AuditEventImmutable("UPDATE")


def _on_before_delete(_mapper: Mapper, _connection: object, _target: object) -> None:
    raise AuditEventImmutable("DELETE")


def install_audit_immutability_listener() -> bool:
    """Install the ``before_update`` / ``before_delete`` listeners on ``AuditEvent``.

    Returns True iff the listener was installed. Idempotent: a second
    call is a no-op. Wired from the composition root (``app.main``) and
    the seed CLI — never from ``app.db`` (T039 layer contract).

    Why at the mapper level (not the session level): mapper events
    fire for every flush of an ``AuditEvent`` regardless of session
    identity, which is the layer the AGENTS.md §4.R12 audit contract
    requires.
    """
    global _LISTENER_INSTALLED  # noqa: PLW0603  (intentional module state)
    if _LISTENER_INSTALLED:
        return True

    event.listen(AuditEventRow, "before_update", _on_before_update, propagate=True)
    event.listen(AuditEventRow, "before_delete", _on_before_delete, propagate=True)

    _LISTENER_INSTALLED = True
    return True


def reset_audit_immutability_listener() -> None:
    """Test helper: detach the listeners so a fresh install can succeed.

    Why: tests toggle between scenarios that need the listener (the
    immutability tests) and scenarios that exercise the SQL adapter
    directly. A reset keeps state predictable.
    """
    global _LISTENER_INSTALLED  # noqa: PLW0603  (intentional module state)
    # SQLAlchemy's ``event.remove`` raises InvalidRequestError when the
    # listener was never registered; that is the expected first-call
    # state, so we suppress it. Production callers never hit this path
    # because the listener is installed once at engine construction time.
    with contextlib.suppress(Exception):  # noqa: BLE001  (SA raises InvalidRequestError on missing listener)
        event.remove(AuditEventRow, "before_update", _on_before_update)
    with contextlib.suppress(Exception):  # noqa: BLE001
        event.remove(AuditEventRow, "before_delete", _on_before_delete)
    _LISTENER_INSTALLED = False


def _execute_state_guard(state: object) -> None:
    """Block raw ORM-execute UPDATE/DELETE against ``AuditEvent``.

    Reserved seam: a future SA upgrade that exposes a portable
    per-engine ``do_orm_execute`` hook can wire this into
    ``install_audit_immutability_execute_listener``. Currently
    unused because SA's ``do_orm_execute`` dispatch is not
    accessible on a sqlite+aiosqlite ``AsyncEngine`` — the DB
    trigger (Layer 1) catches the raw-SQL path instead.

    Accepts an opaque state argument so the signature matches
    SA's ``ORMExecuteState`` contract regardless of which SA
    version the future caller is on.
    """
    statement = getattr(state, "statement", None)
    if statement is None:
        return
    if not isinstance(statement, (update, delete)):
        return
    entity: Any = getattr(statement, "table", None)
    if entity is not None and getattr(entity, "name", None) == "audit_events":
        raise AuditEventImmutable("UPDATE" if isinstance(statement, update) else "DELETE")


def install_audit_immutability_execute_listener(engine: object) -> bool:
    """Install the ``do_orm_execute`` guard for raw UPDATE/DELETE on ``AuditEvent``.

    Wired from the composition root (``app.main``). Returns True iff installed.

    Note: this listener is currently a no-op stub. The four-layer
    append-only contract is satisfied by:

    1. The DB trigger (alembic migration ``0002``) — rejects raw
       ``UPDATE``/``DELETE`` at the SQL layer.
    2. The mapper-level ``before_update`` / ``before_delete``
       listeners installed by ``install_audit_immutability_listener``
       — reject dirty-flush updates through the ORM unit-of-work.
    3. No application code path that issues UPDATE/DELETE.

    A raw ORM ``session.execute(update(AuditEvent))`` bypasses the
    unit-of-work and would not fire the mapper listener; the DB
    trigger catches it instead. We keep this stub so a future SA
    upgrade that exposes a portable per-engine ``do_orm_execute``
    hook can drop in here without changing callers.

    The ``engine`` parameter is intentionally accepted (but unused)
    so the call site in ``app/db.py`` can hand the engine through
    without branching on whether the listener is a no-op.
    """
    del engine
    return True


__all__ = [
    "AuditEventImmutable",
    "install_audit_immutability_execute_listener",
    "install_audit_immutability_listener",
    "reset_audit_immutability_listener",
]
