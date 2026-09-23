"""Domain tests for the FM-039 H0 refusal seam on the Case aggregate
(P08-US1).

The H0 slice ships a single ``legal_hold`` boolean flag on the
``Case`` aggregate. Any attempt to flip a held case to a terminal
status (``closed`` / ``cancelled``) raises :class:`Conflict` so the
HTTP boundary can return a structured 409 to the client. H1 will
replace the flag with a per-case hold record (actor / reason /
released_at / released_by); the guard at the aggregate stays.

These tests are pure-domain — no I/O, no DB. They mirror the
:mod:`tests.domain.test_sla` style: a fixed tenant, a fixed now,
and a single Case built with the published-case-type factory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
)
from app.domain.errors import Conflict

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _schema() -> CaseTypeSchema:
    return CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=(),
        rules=(),
    )


def _published_version() -> CaseTypeVersion:
    return CaseTypeVersion.draft(
        tenant_id=ACME_TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=_schema(),
        now=NOW,
    ).publish(at=NOW, actor_id=ACTOR)


def _case(*, legal_hold: bool = False) -> Case:
    version = _published_version()
    ref = CaseReference("C-ACME-2026-000001")
    return Case.create(
        tenant_id=ACME_TENANT,
        reference=ref,
        case_type_version=version,
        title="t",
        description="",
        data={"title": "t"},
        created_by=ACTOR,
        now=NOW,
        legal_hold=legal_hold,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create__legal_hold_defaults_to_false() -> None:
    """A freshly created case is not under legal hold."""
    case = _case()
    assert case.legal_hold is False


# ---------------------------------------------------------------------------
# Refusal: legal_hold + terminal status
# ---------------------------------------------------------------------------


def test_change_status__refuses_closing_held_case() -> None:
    """The FM-039 H0 refusal: a held case cannot be closed.

    The HTTP boundary maps :class:`Conflict` to 409, so a client
    attempting to close a held case gets a structured refusal, not
    a silent no-op."""
    case = _case(legal_hold=True)
    with pytest.raises(Conflict, match="legal hold"):
        case.change_status(new_status=CaseStatus.CLOSED, now=NOW)


def test_change_status__refuses_cancelling_held_case() -> None:
    """Cancellation is also a terminal disposition; the same guard
    applies (FM-039 — the refusal is on disposition, not just on
    close)."""
    case = _case(legal_hold=True)
    with pytest.raises(Conflict, match="legal hold"):
        case.change_status(new_status=CaseStatus.CANCELLED, now=NOW)


def test_change_status__allowed_for_held_case_when_non_terminal() -> None:
    """A held case is not frozen — the worker can still record
    notes, add documents, transition through non-terminal
    workflow states. Only the terminal flip is refused."""
    case = _case(legal_hold=True)
    updated = case.change_status(new_status=CaseStatus.IN_PROGRESS, now=NOW)
    assert updated.status is CaseStatus.IN_PROGRESS


def test_change_status__allowed_when_legal_hold_false() -> None:
    """The normal close path is untouched: a case without a
    legal hold closes with the regular 1-token bump."""
    case = _case(legal_hold=False)
    closed = case.change_status(new_status=CaseStatus.CLOSED, now=NOW)
    assert closed.status is CaseStatus.CLOSED
    assert closed.closed_at == NOW
    assert closed.version_token == case.version_token + 1


def test_change_status__raises_for_terminal_held_case_message_includes_reference() -> None:
    """The error message carries the case reference so the
    operator can see which case is being refused without
    round-tripping the API."""
    case = _case(legal_hold=True)
    with pytest.raises(Conflict) as excinfo:
        case.change_status(new_status=CaseStatus.CLOSED, now=NOW)
    assert "C-ACME-2026-000001" in str(excinfo.value)
