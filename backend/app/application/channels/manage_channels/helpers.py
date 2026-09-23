"""Shared helpers: payload-hash provenance, audit append, user proxy, policy dict.

One small module so :mod:`.admin` and :mod:`.quarantine` share the audit /
provenance plumbing without importing each other. Every helper here is
value-preserving with respect to Constitution V: diagnostic contexts carry
keys and hashes — never payload values. Split out of the former
single-file ``manage_channels.py`` during the R2 package split.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.channel import ChannelKind, ValidationPolicy
from app.domain.ports import AuditEventStore
from app.domain.provenance import compute_payload_hash

from .constants import SUBJECT_TYPE_CHANNEL, SUBJECT_TYPE_QUARANTINE


def _canonical_values_bytes(values: Mapping[str, str]) -> bytes:
    """Return deterministic bytes for ``values`` for payload-hash provenance.

    Args:
        values: The captured row's string map (keys + values).

    Returns:
        UTF-8 JSON bytes with sorted keys and compact separators, so the
        same logical row always hashes identically regardless of dict
        insertion order.

    Why JSON and not ``str(values)``: ``str(dict)`` embeds insertion
    order and Python repr quoting, so two equal rows could hash
    differently and break operator reconciliation. The canonical form
    mirrors :func:`app.domain.audit.canonical_json` and
    :func:`app.domain.webhook_capture.payload_digest`.
    """
    # Why ``ensure_ascii=False`` + UTF-8: synthetic labels may carry
    # non-ASCII (e.g. em-dash in source labels); hashing the UTF-8 form
    # keeps the digest stable across JSON encoders.
    return json.dumps(
        dict(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _poison_context(
    *,
    decision_context: Mapping[str, Any],
    channel_kind: ChannelKind,
    values: Mapping[str, str],
) -> dict[str, Any]:
    """Build the quarantine ``context`` for a poison row without leaking values.

    Args:
        decision_context: The diagnostic keys from ``evaluate_row``
            (e.g. ``{"missing": ["title"]}``) — keys only, never values.
        channel_kind: The channel the row arrived on; recorded as
            ``source_system`` so the operator console can bucket by
            surface without joining the channel row.
        values: The captured row, used *only* to compute the
            ``payload_hash`` fingerprint.

    Returns:
        A new dict with the decision keys plus ``payload_hash`` (64-char
        SHA-256 hex over the canonical row bytes) and ``source_system``.

    Why a hash and not the values: the holding area is queryable by
    operators who may lack the payload read grant (Constitution V).
    The hash lets them prove "this is the payload we received" during
    reconciliation without the audit trail ever embedding PII.
    """
    # Why copy first: ``decision.context`` belongs to the pure-domain
    # verdict; mutating it would leak application provenance back into
    # the domain object.
    enriched: dict[str, Any] = dict(decision_context)
    enriched["payload_hash"] = compute_payload_hash(_canonical_values_bytes(values))
    enriched["source_system"] = channel_kind.value
    return enriched


def _payload_hash_from_context(context: Mapping[str, Any]) -> str:
    """Return the ``payload_hash`` passthrough for release/reject audit rows.

    Args:
        context: The held record's diagnostic context.

    Returns:
        The stored 64-char hex digest, or ``""`` when the record predates
        payload-hash provenance (backwards compatibility for rows
        quarantined before this slice). Never raises — audit emission
        must not fail on old rows.
    """
    value = context.get("payload_hash", "")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _user_context(cmd: Any) -> Any:
    """Build the policy-facing user context from a command's actor fields.

    Args:
        cmd: A frozen command carrying ``tenant_id``, ``actor_id`` and
            ``actor_roles`` (every entry-point command in this package).

    Returns:
        A :class:`_UserProxy` the authorization policy can evaluate.
        Typed ``Any`` to mirror the pre-split duck-typing contract.
    """
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


async def _emit_audit(
    audit: AuditEventStore,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    subject_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str,
    occurred_at: Any,
) -> None:
    """Append one audit row with the channel/quarantine subject derived.

    Args:
        audit: The append-only audit store port.
        tenant_id: Tenant the event belongs to.
        actor_id: The user on whose behalf the event occurred.
        subject_id: The channel or quarantine-record id being described.
        event_type: Stable event string (``Channel*`` vs quarantine).
        payload: Safe diagnostic payload — keys/hashes only, no values.
        correlation_id: Request correlation id for the timeline.
        occurred_at: UTC timestamp from the injected clock.

    Returns:
        ``None``. Failures propagate so the surrounding transaction
        rolls back — a mutation must never commit without its audit row.
    """
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type=(
            SUBJECT_TYPE_CHANNEL if event_type.startswith("Channel") else SUBJECT_TYPE_QUARANTINE
        ),
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=occurred_at,
    )


def _policy_to_dict(policy: ValidationPolicy) -> dict[str, Any]:
    """Render a :class:`ValidationPolicy` as a JSON-safe audit payload.

    Args:
        policy: The channel's validation policy (enums flattened to their
            ``.value`` strings so the audit row is stable across code
            changes).

    Returns:
        A plain dict of lists / scalars / enum values.
    """
    return {
        "required_fields": list(policy.required_fields),
        "allowed_classifications": list(policy.allowed_classifications),
        "max_record_bytes": policy.max_record_bytes,
        "missing_field_severity": policy.missing_field_severity.value,
        "unknown_field_severity": policy.unknown_field_severity.value,
        "invalid_enum_severity": policy.invalid_enum_severity.value,
    }


__all__ = [
    "_UserProxy",
    "_canonical_values_bytes",
    "_emit_audit",
    "_payload_hash_from_context",
    "_poison_context",
    "_policy_to_dict",
    "_user_context",
]
