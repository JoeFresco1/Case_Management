"""Database engine, session factory, and naming conventions.

Persistence uses SQLAlchemy generic types only (``sqlalchemy.Uuid``,
``sqlalchemy.JSON``, ``sqlalchemy.DateTime(timezone=True)``). No
dialect-specific types in domain code.

# Tenant-isolation listener (FM-002 / P11-US2)

This module also installs a SQLAlchemy ORM-execute hook that injects a
``WHERE tenant_id = :current_tenant_id`` clause into statements that
target an ORM entity with a ``tenant_id`` column. The listener is
**defense-in-depth**:

- **On**: it adds the tenant filter on top of whatever the repository
  already wrote. A correctly-written repository is unaffected; a buggy
  one still cannot leak cross-tenant rows.
- **Off**: it does nothing. The repository's explicit
  ``WHERE tenant_id = :tenant_id`` is the only filter.

It is gated by ``settings.tenant_listener_enabled`` and by an explicit
per-session "current tenant" set via ``set_current_tenant(session, ...)``.
If no current tenant is set, the listener does not inject anything
(rather than silently deny).

A unit test in ``tests/security/test_tenant_isolation.py`` exercises the
listener on and off to prove the explicit filter alone is sufficient.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, with_loader_criteria

from app.config import Settings, get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Tenant listener — opt-in defense-in-depth
# ---------------------------------------------------------------------------


# Per-session registry of "current tenant". Stored on the AsyncSession via
# ``info``; never trusted across requests unless explicitly set.
_SESSION_TENANT_KEY = "current_tenant_id"
_LISTENER_INSTALLED = False


def set_current_tenant(session: AsyncSession, tenant_id: UUID | None) -> None:
    """Pin a session to a tenant. The tenant listener (if enabled) will
    inject ``WHERE tenant_id = :current_tenant_id`` on every ORM query
    that targets a model with a ``tenant_id`` column.

    Pass ``None`` to clear (the listener becomes a no-op).
    """
    session.info[_SESSION_TENANT_KEY] = tenant_id


def get_current_tenant(session: AsyncSession) -> UUID | None:
    return session.info.get(_SESSION_TENANT_KEY)


def install_tenant_listener(_engine: AsyncEngine, *, settings: Settings) -> bool:
    """Install the tenant-isolation ORM listener.

    The engine argument is retained for backwards compatibility with
    existing call sites; the listener registers on the ORM ``Session``
    class (see comment below), so no per-engine handle is needed.

    Returns True iff the listener was installed. The listener is a no-op
    unless both:

    1. ``settings.tenant_listener_enabled`` is True, and
    2. the session has been pinned via ``set_current_tenant``.

    Why opt-in: tests want to prove the explicit filter alone is enough,
    so we keep the listener behind a flag and off during security tests.
    """
    global _LISTENER_INSTALLED  # noqa: PLW0603
    if _LISTENER_INSTALLED:
        return True
    if not settings.tenant_listener_enabled:
        return False

    from sqlalchemy import event  # noqa: PLC0415
    from sqlalchemy.orm import ORMExecuteState, Session  # noqa: PLC0415

    # Why ``Session`` and not the engine: ``do_orm_execute`` is an ORM
    # Session event (it does not exist on engines). ``AsyncSession``
    # delegates ORM execution to its underlying sync ``Session``, so a
    # listener on ``Session`` also fires for async statements, with
    # ``state.session`` being the sync session whose ``info`` dict is
    # shared with the ``AsyncSession`` proxy (``set_current_tenant``
    # writes there). Registering on ``AsyncSession`` raises
    # ``AttributeError: No such event`` — verified against sqlite.
    @event.listens_for(Session, "do_orm_execute")
    def _add_tenant_criteria(state: ORMExecuteState) -> None:
        # Only act when the session has a current tenant pinned.
        session = state.session
        if session is None:
            return
        tenant_id = session.info.get(_SESSION_TENANT_KEY)
        if tenant_id is None:
            return

        # Per-entity loader criteria. SQLAlchemy 2.x requires
        # ``with_loader_criteria(entity, where_criteria)`` — the earlier
        # single-callable form raised ``TypeError`` on *every*
        # tenant-pinned execution (it surfaced as HTTP 500 on idempotent
        # mutations whenever a prior test had booted the app with the
        # listener enabled). Entities come from the statement's
        # ``column_descriptions`` so Core DML executed through the
        # session (e.g. a ``Table.insert()`` with no ORM entity, as the
        # idempotency tests use) passes through untouched.
        descriptions: Any = getattr(state.statement, "column_descriptions", None)
        if not descriptions:
            return
        criteria = [
            with_loader_criteria(entity, entity.tenant_id == tenant_id)
            for desc in descriptions
            if (entity := desc.get("entity")) is not None
            and isinstance(entity, type)
            and hasattr(entity, "tenant_id")
        ]
        if not criteria:
            return
        with suppress(ArgumentError):
            state.statement = state.statement.options(*criteria)
            # Best-effort: if the statement cannot accept the option, leave
            # it alone. The explicit repository filter is still authoritative.
            # Only ArgumentError is suppressed (statement type doesn't support
            # loader criteria); other errors propagate so real failures
            # are not silently swallowed.

    _LISTENER_INSTALLED = True
    return True


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            future=True,
        )
        install_tenant_listener(_engine, settings=settings)
        # NOTE (T039): append-only audit enforcement used to be installed
        # here, which made ``app.db`` import ``app.infrastructure`` and
        # broke the "Interfaces do not import infrastructure" contract
        # transitively (interfaces -> deps -> db -> infrastructure). The
        # guards now live with the composition root: ``app.main`` installs
        # them at startup and the seed CLI installs them before seeding
        # (see ``app.infrastructure.persistence.audit_events``).
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker  # noqa: PLW0603
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Use this in scripts and the outbox dispatcher."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def reset_engine() -> None:
    """Test helper: drop the cached engine and session factory.

    Also clears the ``_LISTENER_INSTALLED`` flag so tests that toggle
    ``tenant_listener_enabled`` between cases can re-install the listener
    cleanly.

    NOTE (T039): this no longer touches the audit-immutability listeners.
    Those are process-global mapper listeners owned by the composition
    root (``app.main``) and by ``tests/audit/test_immutability.py``, which
    installs/resets them explicitly. Importing their module here would
    reintroduce the ``app.db -> app.infrastructure`` edge the layer
    contract forbids.
    """
    global _engine, _sessionmaker, _LISTENER_INSTALLED  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _LISTENER_INSTALLED = False
