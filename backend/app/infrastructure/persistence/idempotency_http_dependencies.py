"""Atomic SQL reservations and replay responses for local HTTP mutations.

INTEGRATION_SEAM: Azure SQL — portable transactions and a composite tenant/key
constraint provide the same reservation contract for a future adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncTransaction, async_sessionmaker

from app.application.idempotency.http_dependencies import IdempotencyTransaction
from app.db import get_sessionmaker, set_current_tenant
from app.domain.errors import DependencyUnavailable
from app.infrastructure.persistence.models import IdempotencyKey


class SqlIdempotencyTransaction:
    """Hold the route session until its response has been persisted successfully."""

    def __init__(
        self, session: AsyncSession, transaction: AsyncTransaction, row: IdempotencyKey
    ) -> None:
        """Bind a reserved row and the externally owned SQL transaction."""
        self.session = session
        self.prior: dict[str, object] | None = None
        self._transaction = transaction
        self._row = row

    async def finalize(
        self, *, response_status: int, response_body: object, correlation_id: str
    ) -> None:
        """Commit domain writes and replay together; reject a rolled-back request."""
        if not self._transaction.is_active:
            raise DependencyUnavailable("request transaction was rolled back")
        self._row.response_status = response_status
        self._row.response_body = cast(dict[str, object], response_body)
        self._row.correlation_id = correlation_id
        self.session.add(self._row)
        await self.session.flush()
        await self._transaction.commit()


class SqlIdempotencyHttpDependencyProvider:
    """Reserve keys with database uniqueness rather than a process-local lock."""

    def __init__(self, *, sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
        """Accept an isolated test factory; otherwise use configured persistence."""
        self._sessionmaker = sessionmaker

    @asynccontextmanager
    async def transaction(
        self, *, tenant_id: UUID, key: str, request_hash: str, correlation_id: str
    ) -> AsyncIterator[IdempotencyTransaction]:
        """Serialize matching keys and roll back every unfinalized request.

        The reservation insert is the first database operation. Concurrent callers
        wait on its unique constraint and can only replay a committed response.
        A crash rolls back both the reservation and the mutation.
        """
        factory = self._sessionmaker or get_sessionmaker()
        try:
            async with factory() as owner:
                engine = owner.bind
                if engine is None:
                    raise DependencyUnavailable("idempotency database is not configured")
                async with engine.connect() as connection:
                    outer = await connection.begin()
                    # Route commit() flushes but cannot commit the external transaction.
                    async with AsyncSession(
                        bind=connection,
                        expire_on_commit=False,
                        join_transaction_mode="rollback_only",
                    ) as session:
                        set_current_tenant(session, tenant_id)
                        reservation = await self._reserve(
                            session,
                            outer,
                            tenant_id=tenant_id,
                            key=key,
                            request_hash=request_hash,
                            correlation_id=correlation_id,
                        )
                        try:
                            yield reservation
                        finally:
                            if connection.in_transaction():
                                await connection.rollback()
        except SQLAlchemyError as exc:
            # SQL and driver values must not leak through HTTP errors.
            raise DependencyUnavailable("idempotency persistence unavailable") from exc

    async def _reserve(
        self,
        session: AsyncSession,
        outer: AsyncTransaction,
        *,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        correlation_id: str,
    ) -> SqlIdempotencyTransaction:
        """Insert first so a simultaneous duplicate cannot execute a second mutation."""
        row = IdempotencyKey(
            tenant_id=tenant_id,
            key=key,
            request_hash=request_hash,
            response_status=0,
            response_body={},
            correlation_id=correlation_id,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            connection = session.bind
            if connection is None:
                raise DependencyUnavailable("idempotency connection unavailable") from None
            outer = await connection.begin()
            prior = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key
                    )
                )
            ).scalar_one_or_none()
            if prior is None or prior.response_status == 0:
                raise DependencyUnavailable("idempotency reservation unavailable") from None
            reservation = SqlIdempotencyTransaction(session, outer, prior)
            reservation.prior = {
                "request_hash": prior.request_hash,
                "response_status": prior.response_status,
                "response_body": prior.response_body,
                "correlation_id": prior.correlation_id,
            }
            return reservation
        return SqlIdempotencyTransaction(session, outer, row)


__all__ = ["SqlIdempotencyHttpDependencyProvider", "SqlIdempotencyTransaction"]
