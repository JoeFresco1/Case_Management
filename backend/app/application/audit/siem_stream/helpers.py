"""Shared row projections and clock/id helpers for the SIEM surface (P09-US8, US9).

The persistence rows are projected once and consumed by both SIEM
surfaces: :func:`_row_to_stream_event` feeds the best-effort stream and
the signed-batch ``payload`` entries, while :func:`_row_to_chain` feeds
the domain chain verifier before a batch is signed. The projections are
identical on purpose — a streamed row and a signed-batch entry must
carry the same ``prev_hash`` / ``hash`` columns so an auditor can
verify both surfaces with one toolchain. Split out of the former single
``siem_stream`` module by the R2 structural refactor; everything here is
re-exported from ``app.application.audit.siem_stream`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict, cast
from uuid import UUID


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Adapter so a command/query dataclass can drive ``authorize``.

    Mirrors the proxy used in ``export_audit`` / ``correct_audit``.
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


class _ChainRow(TypedDict, total=False):
    """The fields required by the domain audit-chain verifier."""

    id: Any
    hash: str
    prev_hash: str
    payload: dict[str, Any]


def _row_to_stream_event(row: Any) -> dict[str, Any]:
    """Project a persistence row into the wire shape consumed by SIEMs.

    The shape is the same as the export entry: id, prev_hash, hash,
    payload, correlation, and the durable timestamps. We include
    ``prev_hash`` and ``hash`` so a downstream verifier can rebuild
    the chain from the stream alone if the auditor wants to.
    """

    def _get(name: str, default: Any = None) -> Any:
        return getattr(row, name, default)

    occurred_at = _get("occurred_at")
    return {
        "id": str(_get("id")),
        "tenant_id": str(_get("tenant_id")),
        "event_type": _get("event_type"),
        "occurred_at": occurred_at.isoformat() if occurred_at is not None else None,
        "actor_id": str(_get("actor_id")),
        "actor_type": _get("actor_type"),
        "subject_type": _get("subject_type"),
        "subject_id": str(_get("subject_id")),
        "case_id": str(_get("case_id")) if _get("case_id") is not None else None,
        "correlation_id": _get("correlation_id"),
        "causation_id": _get("causation_id"),
        "payload": _get("payload") or {},
        "prev_hash": _get("prev_hash"),
        "hash": _get("hash"),
    }


def _row_to_chain(row: Any) -> _ChainRow:
    """Project a persistence row into the chain-verifier shape.

    Returns a dict (the shape ``app.domain.audit.verify_chain``
    expects via its ``_ChainRow`` TypedDict). A dataclass is not
    subscriptable on attribute lookup, so a dict is the portable
    shape across the in-memory fake, the SQL adapter, and the
    auditor's off-system verification tooling.
    """

    return {
        "id": row.id,
        "hash": row.hash,
        "prev_hash": row.prev_hash,
        "payload": dict(getattr(row, "payload", None) or {}),
    }


def _row_to_signed_entry(row: Any) -> dict[str, Any]:
    """The row shape embedded in the signed batch ``payload``.

    The auditor recomputes the chain from these entries using the
    same ``prev_hash`` / ``hash`` columns; the signature attests
    that the platform held the secret at ``created_at``.
    """

    return _row_to_stream_event(row)


def _now_utc() -> datetime:
    """Current UTC time for batch ``created_at`` when no ``Clock`` is injected.

    Deferred imports keep the module import cheap and mirror the
    pre-split module; the caller passes a ``Clock`` in tests so the
    timestamp stays deterministic there.
    """

    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    return _dt.now(UTC)


def _new_batch_id() -> UUID:
    """Mint a batch id with the same uuid7 convention the rest of the codebase uses.

    Why uuid7: sortable, time-ordered ids make the Gated
    signed-batch timeline readable to an off-system auditor without
    an additional ``created_at`` sort. Falls back to ``uuid4`` if
    ``uuid_extensions`` is not installed.
    """

    try:
        from uuid_extensions import uuid7  # noqa: PLC0415

        return cast(UUID, uuid7())
    except ImportError:  # pragma: no cover - defensive
        from uuid import uuid4  # noqa: PLC0415

        return uuid4()


__all__ = [
    "_ActorProxy",
    "_ChainRow",
    "_new_batch_id",
    "_now_utc",
    "_row_to_chain",
    "_row_to_signed_entry",
    "_row_to_stream_event",
    "TypedDict",
    "cast",
    "dataclass",
    "datetime",
]
