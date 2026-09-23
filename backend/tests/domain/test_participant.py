"""Business tests for participant identity and history rules."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.errors import InvariantViolation
from app.domain.participant import Participant


def _participant() -> Participant:
    """Build a valid synthetic participant fixture."""
    return Participant.create(
        tenant_id=uuid4(),
        case_id=uuid4(),
        role="requester",
        display_name="Alex Example",
        source="synthetic",
        is_primary=True,
        added_at=datetime.now(UTC),
    )


def test_participant_remove__preserves_identity_and_metadata() -> None:
    participant = _participant()
    removed = participant.remove(removed_at=participant.added_at + timedelta(minutes=1))

    assert removed.id == participant.id
    assert removed.removed_at is not None
    assert removed.display_name == participant.display_name


def test_participant_create__rejects_blank_required_values() -> None:
    with pytest.raises(InvariantViolation):
        Participant.create(
            tenant_id=uuid4(),
            case_id=uuid4(),
            role=" ",
            display_name="Alex Example",
            source="synthetic",
            is_primary=False,
            added_at=datetime.now(UTC),
        )


def test_participant_remove__rejects_timestamp_before_addition() -> None:
    participant = _participant()
    with pytest.raises(InvariantViolation):
        participant.remove(removed_at=participant.added_at - timedelta(seconds=1))
