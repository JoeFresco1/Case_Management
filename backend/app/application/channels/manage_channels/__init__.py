"""Channel + quarantine use cases (P04-US4, US5 / FM-027).

This package is the application layer for ingestion-channel governance
and quarantine. It is intentionally narrow: a single ``Channel`` may
be enabled, disabled, or have its policy replaced; a single captured
item may be quarantined, inspected, released, or rejected.

# Why a package (R2 structural split)

Formerly a single ``manage_channels.py`` module of ~890 lines — past the
R2 600-line soft limit. Split by responsibility into :mod:`constants`
(audit event + subject types), :mod:`helpers` (payload-hash provenance,
audit append, user proxy, policy dict), :mod:`commands` (frozen
command/result dataclasses), :mod:`admin` (enable / disable), and
:mod:`quarantine` (capture / inspect / release / reject). This
``__init__`` is a re-export shim: ``__all__`` and every previously
visible attribute are re-exported unchanged, so no consumer import line
needs editing and ``dir()`` stays identical.

# Why a per-row quarantine (P04 D4)

The acceptance criterion from pillar P04 §6 D4 is that a single bad
row never blocks the good ones. ``quarantine_capture`` enforces this
by routing a captured row through ``app.domain.channel.evaluate_row``
in isolation: a failure produces a ``QuarantineRecord`` for that row,
while sibling rows in the same batch continue. The operator console
queries ``inspect_quarantine`` to find the held row, then either
``release`` (re-process with a correction) or ``reject`` (drop
permanently).

# Authorization

Every entry point requires ``ADMIN_TENANT_CONFIG``. A process_admin
grants it; ``intake`` / ``reviewer`` / ``supervisor`` do not.
``release`` and ``reject`` also require ``ADMIN_TENANT_CONFIG`` —
quarantine resolution is an operator-only action.

# Multi-tenant posture

Every method takes ``tenant_id`` from the ``UserContext``. Cross-tenant
calls are denied at the policy layer; ``ChannelRepository.get`` and
``QuarantineRepository.get`` return ``None`` for cross-tenant ids,
which the use case translates to ``NotFound``.

# Synthetic posture

Records carry ``source_label`` and ``channel_key`` so the operator UI
can render the queue without ever embedding the row's payload data
(Constitution V). The diagnostic ``context`` dict on a record holds
keys (e.g. ``{"missing": ["title"]}``) — never values.
"""

from __future__ import annotations

import json  # noqa: F401
from collections.abc import Mapping  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from typing import Any, Final  # noqa: F401
from uuid import UUID, uuid4  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.domain.channel import (  # noqa: F401
    Channel,
    ChannelKind,
    PolicyDecision,
    QuarantineRecord,
    QuarantineStatus,
    ValidationPolicy,
    ValidationSeverity,
    evaluate_row,
)
from app.domain.errors import NotFound  # noqa: F401
from app.domain.errors import ValidationFailed as DomainValidationFailed  # noqa: F401
from app.domain.ports import (  # noqa: F401
    AuditEventStore,
    ChannelRepository,
    OutboxWriter,
    QuarantineRepository,
)
from app.domain.provenance import compute_payload_hash  # noqa: F401

from .admin import disable_channel, enable_channel  # noqa: F401
from .commands import (  # noqa: F401
    DisableChannelCommand,
    DisableChannelResult,
    EnableChannelCommand,
    EnableChannelResult,
    InspectQuarantineQuery,
    InspectQuarantineResult,
    QuarantineCaptureCommand,
    QuarantineCaptureResult,
    RejectQuarantineCommand,
    RejectResult,
    ReleaseQuarantineCommand,
    ReleaseResult,
)
from .constants import (  # noqa: F401
    EVENT_CAPTURE_QUARANTINED,
    EVENT_CAPTURE_REJECTED,
    EVENT_CHANNEL_DISABLED,
    EVENT_CHANNEL_ENABLED,
    EVENT_CHANNEL_POLICY_UPDATED,
    EVENT_QUARANTINE_REJECTED,
    EVENT_QUARANTINE_RELEASED,
    SUBJECT_TYPE_CHANNEL,
    SUBJECT_TYPE_QUARANTINE,
)
from .helpers import (  # noqa: F401
    _canonical_values_bytes,
    _emit_audit,
    _payload_hash_from_context,
    _poison_context,
    _policy_to_dict,
    _user_context,
    _UserProxy,
)
from .quarantine import (  # noqa: F401
    inspect_quarantine,
    quarantine_capture,
    reject_quarantine,
    release_quarantine,
)

# Error codes used by the channel-level validation. Mirrors the
# import-pipeline vocabulary so the operator UI buckets consistently.
# Why defined here rather than in :mod:`constants`: these four annotated
# assignments populate this package's ``__annotations__`` exactly as the
# pre-split module did, keeping ``dir()`` byte-identical. They are pure
# exports — nothing inside the package references them.
ERROR_MISSING_REQUIRED_FIELD: Final[str] = "missing_required_field"
ERROR_UNKNOWN_FIELD: Final[str] = "unknown_field"
ERROR_INVALID_ENUM: Final[str] = "invalid_enum"
ERROR_SIZE_EXCEEDED: Final[str] = "size_exceeded"

__all__ = [
    "DisableChannelCommand",
    "DisableChannelResult",
    "EnableChannelCommand",
    "EnableChannelResult",
    "ERROR_INVALID_ENUM",
    "ERROR_MISSING_REQUIRED_FIELD",
    "ERROR_SIZE_EXCEEDED",
    "ERROR_UNKNOWN_FIELD",
    "InspectQuarantineQuery",
    "InspectQuarantineResult",
    "QuarantineCaptureCommand",
    "QuarantineCaptureResult",
    "RejectQuarantineCommand",
    "RejectResult",
    "ReleaseQuarantineCommand",
    "ReleaseResult",
    "disable_channel",
    "enable_channel",
    "inspect_quarantine",
    "quarantine_capture",
    "reject_quarantine",
    "release_quarantine",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.channels.manage_channels)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (they are already in ``sys.modules``, which the
# import system consults before touching ``__path__``); only the redundant
# package-level attributes go away.
for _submodule in (
    "admin",
    "commands",
    "constants",
    "helpers",
    "quarantine",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
