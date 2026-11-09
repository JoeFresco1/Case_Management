"""Synthetic dataset constants and builders (AGENTS.md §18 — no PHI).

Pure data for the seed: the tenant identity constants, the stable-UUID
synthetic user roster, and the ``ServiceRequest`` case-type schema from
AGENTS.md §16.2. No I/O, no session, no settings — the seed runner
(:mod:`app.cli.seed.runner`) consumes these to write rows.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from uuid import UUID

from app.domain.case_type import CaseTypeField, CaseTypeSchema

__all__ = [
    "SyntheticUser",
    "TENANT_KEY",
    "TENANT_NAME",
    "TENANT_SHORT",
    "_service_request_schema",
    "_synthetic_users",
]

# ---------------------------------------------------------------------------
# Synthetic dataset (AGENTS.md §18 — no PHI, example.test domains)
# ---------------------------------------------------------------------------


TENANT_KEY: str = "ACME"
TENANT_NAME: str = "Acme Synthetic Co."
TENANT_SHORT: str = "ACME"


@dataclass(frozen=True, slots=True)
class SyntheticUser:
    """A local-dev user identity.

    ``key`` is what the dev-header IdP matches against ``X-Dev-User``;
    ``user_id`` is the row's UUID. ``roles`` mirror the policy in
    ``app.application.authorization``: intake can create cases, reviewer
    can read them, supervisor can work cases, and process_admin can
    manage tenant configuration.
    """

    key: str
    display_name: str
    email: str
    roles: tuple[str, ...]
    user_id: UUID


def _synthetic_users(tenant_id: UUID) -> tuple[SyntheticUser, ...]:
    # Why stable UUIDs: the dev-header IdP needs to resolve a known
    # UUID for ``X-Dev-User=intake`` even after the DB is wiped and
    # re-seeded; using ``uuid4()`` would leave stale dev sessions
    # pointing at non-existent rows. We derive them deterministically
    # from a namespace UUID so re-running ``seed`` produces the same
    # identities. ``tenant_id`` is unused at this layer (the UUID is
    # derived from the role key alone) but the parameter is part of the
    # caller's contract; suppress ARG001 explicitly.
    _ = tenant_id

    def _stable(name: str) -> UUID:
        return UUID(int=_uuid.uuid5(_uuid.NAMESPACE_DNS, f"acme.example.test/{name}").int)

    return (
        SyntheticUser(
            key="intake",
            display_name="Alex Intake",
            email="alex.intake@example.test",
            roles=("intake", "case.create"),
            user_id=_stable("intake"),
        ),
        SyntheticUser(
            key="reviewer",
            display_name="Riley Reviewer",
            email="riley.reviewer@example.test",
            roles=("reviewer", "case.read"),
            user_id=_stable("reviewer"),
        ),
        SyntheticUser(
            key="supervisor",
            display_name="Sam Supervisor",
            email="sam.supervisor@example.test",
            roles=("supervisor", "case.create", "case.read", "case.admin"),
            user_id=_stable("supervisor"),
        ),
        SyntheticUser(
            key="process_admin",
            display_name="Pat Process Admin",
            email="pat.process_admin@example.test",
            roles=("process_admin",),
            user_id=_stable("process_admin"),
        ),
    )


def _service_request_schema() -> CaseTypeSchema:
    """Synthetic ServiceRequest schema (AGENTS.md §16.2, no PHI).

    Field set is intentionally tiny: required ``title`` and
    ``priority`` are enough to drive the create_case use case through
    its success + validation paths. Optional ``due_date`` exercises
    the date field type. ``sensitivity=internal`` everywhere — never
    ``phi`` or ``restricted`` — so the seed never produces data that
    triggers redaction in the telemetry layer.
    """
    return CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
                sensitivity="internal",
                searchable=True,
            ),
            CaseTypeField(
                key="priority",
                label="Priority",
                type="enum",
                required=True,
                enum_values=("low", "normal", "high", "urgent"),
                sensitivity="internal",
            ),
            CaseTypeField(
                key="due_date",
                label="Due date",
                type="date",
                required=False,
                sensitivity="internal",
            ),
        ),
        document_classifications=("Contract", "Correspondence", "Evidence"),
        rules=(),
    )
