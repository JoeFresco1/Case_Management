"""Outbox dispatcher: drains ``outbox`` rows into the MessageBus.

# INTEGRATION_SEAM: outbox dispatcher — local in-process loop; production
target is a hosted worker (Azure Functions / Container Apps) consuming
the same ``outbox`` table via the same dispatcher logic. The dispatcher
itself is the same code path in both postures — only the scheduler
differs.

Why a dispatcher at all (AGENTS.md §12, plan §4.R12): the outbox row is
written in the same DB transaction as the domain change. A separate
worker drains rows after commit so that:

- A publisher outage cannot leave the domain change unsaved (the row is
  already durable).
- A duplicate publish on retry is harmless because subscribers are
  required to be idempotent (the row carries a stable ``event_id``).
- The dispatcher is the single integration point — use cases never
  call ``MessageBus.publish`` directly.

Why correlation/causation/occurred_at are read from the payload rather
than from dedicated columns: the existing ``outbox`` model does not
carry those fields; the envelope stores them inside ``payload`` so
the dispatcher can surface them on the wire without a schema
migration. FM-055 acceptance ("subscriber receives event with
correlation_id") is satisfied because ``LocalInProcessMessageBus``
reconstructs the wire shape from the payload below.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.events import find_event_spec
from app.infrastructure.messaging.local_message_bus import LocalInProcessMessageBus
from app.infrastructure.persistence.models import Outbox as OutboxRow

logger = logging.getLogger(__name__)

# Safe-error redaction (FM-052 / Constitution V).
#
# Why redact inside the dispatcher instead of trusting the subscriber:
# ``last_error`` is persisted on the outbox row and surfaced to the
# operator console (``GET /api/v1/admin/outbox``). A subscriber that
# raises ``RuntimeError("downstream rejected ssn=123-45-6789")`` would
# otherwise land that value in the row, the API response, and any log
# line that echoes the row. The pattern below replaces the *value* of
# any ``<sensitive-key>=<token>`` / ``<sensitive-key>: <token>`` pair
# with ``[REDACTED]`` and truncates to 500 chars so the TEXT column
# stays portable. Values are never inspected beyond the key name, so a
# secret value alone (without its key) cannot influence the decision —
# the same posture as ``app.application.telemetry.redaction``.
_SENSITIVE_ERROR_KEYS: tuple[str, ...] = (
    "ssn",
    "phi",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "token",
    "private_key",
    "client_secret",
    "refresh_token",
    "session_id",
    "cookie",
    "bearer",
    "dob",
    "birth",
    "address",
    "phone",
    "email",
    "passport",
    "license",
    "tax_id",
    "medical",
    "health",
    "diagnosis",
)

_ERROR_REDACT_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(?<![a-z0-9_])(?:"
    + "|".join(re.escape(k) for k in _SENSITIVE_ERROR_KEYS)
    + r")[a-z0-9_-]*\s*[:=]\s*\S+"
)

_MAX_ERROR_CHARS = 500


def sanitize_error(error: str) -> str:
    """Return a safe, truncated copy of ``error`` for the outbox row.

    Args:
        error: Raw failure text (usually ``repr(exc)`` from a subscriber).

    Returns:
        The redacted string, capped at 500 chars. Sensitive
        ``key=value`` pairs are replaced with ``key=[REDACTED]``; the
        surrounding message (exception type, stable reason) is preserved
        so the operator can still triage.

    Failure mode:
        Never raises for string input; non-string input is coerced with
        ``str()`` so a misbehaving subscriber cannot break the drain.
    """
    try:
        text = error if isinstance(error, str) else str(error)
    except Exception:  # pragma: no cover - str() on an exception never fails in practice
        return "publish failed [REDACTED]"
    redacted = _ERROR_REDACT_PATTERN.sub(
        lambda m: re.sub(r"(?i)\s*[:=]\s*\S+$", "=[REDACTED]", m.group(0)), text
    )
    return redacted[:_MAX_ERROR_CHARS]


# Bounded retry: a row that fails ``MAX_ATTEMPTS`` times is left with
# ``dispatched_at = NULL`` and ``attempt_count`` at the cap, surfacing
# the dead-letter via ``/api/v1/health`` and the future FM-052
# operator console. We do NOT silently drop rows.
MAX_ATTEMPTS = 5

# Retry backoff (T029): deterministic exponential delay between
# attempts — ``min(cap, base * 2**(attempt-1))``. No jitter, so drain
# timing stays reproducible in tests and the scanner's worst-case lag
# is computable (5s + 10s + 20s + 40s ≈ 75s before dead-letter at the
# fifth failure). Failed rows carry ``next_attempt_at``; the drain
# query skips rows whose time has not come so one poison message
# cannot starve healthy rows behind it in the batch.
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0


def backoff_delay_seconds(attempt: int) -> float:
    """Return the retry delay after ``attempt`` failures (1-based).

    Pure function so tests pin the schedule without touching time.
    """
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2.0 ** max(0, attempt - 1)))


# Per-publish wall-clock cap. A subscriber that hangs would otherwise
# pin the dispatcher loop indefinitely; the row remains durable and
# will be retried on the next tick.
PUBLISH_TIMEOUT_SECONDS = 10.0

# Envelope keys the use cases embed in ``payload`` so the dispatcher
# can surface them on the wire. They are intentionally hidden from
# subscribers (the wire payload does not re-include them under the
# payload dict — see ``LocalInProcessMessageBus.publish``).
_ENVELOPE_CORRELATION = "__correlation_id"
_ENVELOPE_CAUSATION = "__causation_id"
_ENVELOPE_OCCURRED_AT = "__occurred_at"


def _envelope(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    """Strip envelope keys out of the payload; return them as a tuple.

    Why strip: subscribers should not see ``__correlation_id`` nested
    inside the payload — they receive it as a top-level wire field.
    """
    correlation = payload.pop(_ENVELOPE_CORRELATION, None)
    causation = payload.pop(_ENVELOPE_CAUSATION, None)
    occurred = payload.pop(_ENVELOPE_OCCURRED_AT, None)
    return correlation, causation, occurred, payload


def _rebuild_payload(
    wire_payload: dict[str, Any],
    *,
    correlation_id: str | None,
    causation_id: str | None,
    occurred_at: str | None,
) -> dict[str, Any]:
    """Re-inject envelope keys before persisting to ``outbox.payload``.

    Use cases call ``OutboxWriter.enqueue(payload=...)`` directly; the
    dispatcher expects the envelope keys to ride alongside. This
    helper is used by tests to build rows that match what use cases
    produce.
    """
    out = dict(wire_payload)
    if correlation_id is not None:
        out[_ENVELOPE_CORRELATION] = correlation_id
    if causation_id is not None:
        out[_ENVELOPE_CAUSATION] = causation_id
    if occurred_at is not None:
        out[_ENVELOPE_OCCURRED_AT] = occurred_at
    return out


class OutboxDispatcher:
    """Drains ``outbox`` rows into the ``MessageBus``.

    Lifecycle: construct once at startup with a session factory and a
    bus instance; call ``tick`` on an interval (driven by the FastAPI
    startup task in ``main.py``) or ``drain_once`` from a CLI /
    one-shot test. ``tick`` runs forever until cancelled.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: LocalInProcessMessageBus,
        sleep_seconds: float = 1.0,
    ) -> None:
        self._sm = sessionmaker
        self._bus = bus
        self._sleep = sleep_seconds

    async def tick(self) -> None:
        """Run ``drain_once`` on an interval until cancelled.

        Why a separate tick loop rather than a background asyncio
        task in ``main``: keeping the loop in this class makes the
        dispatcher testable in isolation (the tests call
        ``drain_once`` directly) and keeps ``main`` from owning a
        long-running task it cannot cleanly cancel on shutdown.
        """
        try:
            while True:
                try:
                    await self.drain_once()
                except Exception:  # noqa: BLE001 — loop must not die
                    logger.exception("outbox tick failed")
                await asyncio.sleep(self._sleep)
        except asyncio.CancelledError:
            return

    async def drain_once(self, *, batch_size: int = 50) -> int:
        """Drain up to ``batch_size`` pending rows; return count dispatched.

        Why a batch cap: an unbounded drain would hold a long
        transaction and starve the rest of the system. ``batch_size``
        is sized for the local profile (in-proc bus, sqlite + small
        dev DB); the production worker can run a larger cap.
        """
        async with self._sm() as session:
            now = datetime.now(tz=UTC)
            stmt = (
                select(OutboxRow)
                .where(OutboxRow.dispatched_at.is_(None))
                .where(OutboxRow.attempt_count < MAX_ATTEMPTS)
                .where((OutboxRow.next_attempt_at.is_(None)) | (OutboxRow.next_attempt_at <= now))
                .order_by(OutboxRow.created_at.asc())
                .limit(batch_size)
            )
            rows = (await session.execute(stmt)).scalars().all()
            dispatched = 0
            for row in rows:
                ok = await self._dispatch_row(session, row)
                if ok:
                    dispatched += 1
            await session.commit()
            return dispatched

    async def _dispatch_row(self, session: AsyncSession, row: OutboxRow) -> bool:
        """Publish a single outbox row; update its dispatched state.

        Returns ``True`` if the row was marked dispatched. A return
        of ``False`` means publish failed and the row remains
        pending — the next tick retries until ``MAX_ATTEMPTS``.
        """
        # Catalog validation: an unknown event_type must never be
        # published. Marking dispatched on an unknown type would
        # silence a use-case bug; instead, dead-letter the row by
        # capping attempt_count so it shows up in FM-052 triage and
        # is no longer re-attempted.
        if find_event_spec(row.event_type) is None:
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == row.id)
                .values(attempt_count=MAX_ATTEMPTS, last_error="unknown_event_type")
            )
            logger.warning(
                "outbox row with unknown event_type dead-lettered",
                extra={"row_id": str(row.id), "event_type": row.event_type},
            )
            return False

        raw_payload = dict(row.payload or {})
        correlation_id, causation_id, occurred_at, wire_payload = _envelope(raw_payload)

        try:
            await asyncio.wait_for(
                self._bus.publish(
                    tenant_id=row.tenant_id,
                    event_type=row.event_type,
                    event_id=row.event_id,
                    payload=wire_payload,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    occurred_at=occurred_at,
                ),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await self._record_failure(session, row, "publish timeout")
            return False
        except Exception as exc:  # noqa: BLE001 — record and retry
            await self._record_failure(session, row, repr(exc))
            return False

        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == row.id)
            .values(
                dispatched_at=datetime.now(tz=UTC),
                attempt_count=row.attempt_count + 1,
                last_error=None,
                next_attempt_at=None,
            )
        )
        return True

    async def _record_failure(self, session: AsyncSession, row: OutboxRow, error: str) -> None:
        # Sanitize + truncate ``last_error`` to keep the row portable and
        # secret-free; full tracebacks belong in the log line above, not
        # in a TEXT column that the schema cannot constrain and the
        # operator console echoes. The next attempt backs off
        # exponentially from the new attempt count (T029).
        truncated = sanitize_error(error or "")
        new_attempts = row.attempt_count + 1
        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == row.id)
            .values(
                attempt_count=new_attempts,
                last_error=truncated,
                next_attempt_at=datetime.now(tz=UTC)
                + timedelta(seconds=backoff_delay_seconds(new_attempts)),
            )
        )

    async def count_lag(self, *, tenant_id: UUID | None = None) -> dict[str, int]:
        """Return a lag summary used by ``GET /api/v1/admin/outbox``.

        The shape is intentionally small — operators glance at this on
        a dashboard and want a single object. ``dead_lettered`` counts
        rows whose ``attempt_count`` is at the cap (i.e. have given up
        auto-retrying); ``failed_pending`` is rows still under the cap
        but with a non-null ``last_error`` so the operator can see
        retries in flight.

        Args:
            tenant_id: When given, scope every counter to that tenant
                (a tenant operator sees only their own failed work);
                when ``None`` (default) count globally for the platform
                operator console. Explicit scoping is defense-in-depth
                alongside the router's authorization check — the row
                payloads never leave the process here, only counts.
        """
        async with self._sm() as session:
            total_stmt = select(func.count()).select_from(OutboxRow)
            if tenant_id is not None:
                total_stmt = total_stmt.where(OutboxRow.tenant_id == tenant_id)
            pending_stmt = total_stmt.where(OutboxRow.dispatched_at.is_(None))
            dead_stmt = pending_stmt.where(OutboxRow.attempt_count >= MAX_ATTEMPTS)
            failing_stmt = pending_stmt.where(OutboxRow.attempt_count > 0).where(
                OutboxRow.attempt_count < MAX_ATTEMPTS
            )
            total = (await session.execute(total_stmt)).scalar_one()
            pending = (await session.execute(pending_stmt)).scalar_one()
            dead = (await session.execute(dead_stmt)).scalar_one()
            failing = (await session.execute(failing_stmt)).scalar_one()
        return {
            "total": int(total),
            "pending": int(pending),
            "dead_lettered": int(dead),
            "retrying": int(failing),
        }

    async def list_pending(
        self,
        *,
        include_dead_lettered: bool = True,
        limit: int = 50,
        tenant_id: UUID | None = None,
    ) -> list[OutboxRow]:
        """Return pending rows for the operator console.

        By default both in-flight retries AND dead-lettered rows are
        surfaced — the operator needs to see the poison messages so they
        can decide to replay or discard. ``include_dead_lettered=False``
        narrows the response to rows still eligible for auto-retry.
        Order: oldest first (the row that has been waiting longest is
        at the top — AGENTS.md §18 / first-line operator UX).

        Args:
            include_dead_lettered: Include rows at ``MAX_ATTEMPTS``.
            limit: Maximum rows returned (operator page size).
            tenant_id: When given, return only that tenant's rows so a
                tenant-scoped console cannot enumerate another tenant's
                failed work; when ``None`` return globally for the
                platform operator.
        """
        async with self._sm() as session:
            stmt = (
                select(OutboxRow)
                .where(OutboxRow.dispatched_at.is_(None))
                .order_by(OutboxRow.created_at.asc())
                .limit(limit)
            )
            if tenant_id is not None:
                stmt = stmt.where(OutboxRow.tenant_id == tenant_id)
            if not include_dead_lettered:
                stmt = stmt.where(OutboxRow.attempt_count < MAX_ATTEMPTS)
            return list((await session.execute(stmt)).scalars().all())

    async def replay(self, row_id: UUID) -> bool:
        """Reset a row's retry state so the next ``tick`` re-attempts it.

        Returns ``True`` if a row was reset. A row that is already
        dispatched cannot be replayed (returns ``False``); a row that
        never existed also returns ``False``. The replay path is
        idempotent — calling it twice does not duplicate work: the
        second call (before the dispatcher re-attempts) returns
        ``False`` because the row is already eligible (``attempt_count``
        is 0 and no backoff gate is armed). The row itself remains
        durable until successfully dispatched, and redelivery reuses the
        stable ``event_id`` so idempotent subscribers dedupe safely.

        Why reset ``attempt_count`` rather than adding more attempts:
        the operator's intent is "try this again from scratch". The
        previous ``last_error`` is preserved so the operator can see
        the failure history; clearing it would lose audit value.
        """
        async with self._sm() as session:
            row = await session.get(OutboxRow, row_id)
            if row is None or row.dispatched_at is not None:
                return False
            if row.attempt_count == 0 and row.next_attempt_at is None:
                # Already eligible (fresh row or a prior replay that the
                # dispatcher has not re-attempted yet) — no-op so the
                # operator UI can distinguish "reset now" (True) from
                # "already queued for retry" (False).
                return False
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == row_id)
                .where(OutboxRow.dispatched_at.is_(None))
                .values(attempt_count=0, next_attempt_at=None)
            )
            await session.commit()
            return True


__all__ = [
    "OutboxDispatcher",
    "MAX_ATTEMPTS",
    "backoff_delay_seconds",
    "sanitize_error",
    "_rebuild_payload",
    "_envelope",
    "_ENVELOPE_CORRELATION",
    "_ENVELOPE_CAUSATION",
    "_ENVELOPE_OCCURRED_AT",
]
