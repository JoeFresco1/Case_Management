"""Stable audit event types + subject constants for the channel use cases.

The ``EVENT_*`` strings are written to the audit trail and the outbox by
:mod:`.admin` and :mod:`.quarantine`; the ``SUBJECT_TYPE_*`` strings label
audit rows in :func:`.helpers._emit_audit`. Split out of the former
single-file ``manage_channels.py`` during the R2 package split.

Why plain literals and not an Enum: the operator console and timeline
filters key on these exact strings — changing a wire value would orphan
historical audit rows. The validation ``ERROR_*`` codes are pure exports
and live in the package ``__init__`` (see the comment there).
"""

from __future__ import annotations

# Stable event types for the operator's audit timeline.
EVENT_CHANNEL_ENABLED = "ChannelEnabled"
EVENT_CHANNEL_DISABLED = "ChannelDisabled"
EVENT_CHANNEL_POLICY_UPDATED = "ChannelPolicyUpdated"
EVENT_CAPTURE_QUARANTINED = "CaptureQuarantined"
EVENT_CAPTURE_REJECTED = "CaptureRejected"
EVENT_QUARANTINE_RELEASED = "QuarantineReleased"
EVENT_QUARANTINE_REJECTED = "QuarantineRejected"

SUBJECT_TYPE_CHANNEL = "channel"
SUBJECT_TYPE_QUARANTINE = "quarantine_record"

__all__ = [
    "EVENT_CAPTURE_QUARANTINED",
    "EVENT_CAPTURE_REJECTED",
    "EVENT_CHANNEL_DISABLED",
    "EVENT_CHANNEL_ENABLED",
    "EVENT_CHANNEL_POLICY_UPDATED",
    "EVENT_QUARANTINE_REJECTED",
    "EVENT_QUARANTINE_RELEASED",
    "SUBJECT_TYPE_CHANNEL",
    "SUBJECT_TYPE_QUARANTINE",
]
