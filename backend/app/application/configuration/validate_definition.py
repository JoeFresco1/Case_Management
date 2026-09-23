"""Workflow definition validator (FM-013 / FR-015, P02-US2, P10-US1).

This module is the public name referenced by Pillar P10 §3 and by
``specs/001-case-management-platform/first-slice/tasks.md``: the validator
that the workflow ``draft → validate → publish`` lifecycle calls. The
implementation lives in :mod:`app.application.configuration.validate_workflow_definition`
and is re-exported here so callers can import either name.

Why a re-export instead of a copy: the implementation is non-trivial
(reachability, terminal-reachability, SCC-based automatic-cycle
detection, guard operator shape checks). Duplicating it would invite
drift; the single implementation is the source of truth and this module
is the public alias the spec requested.

Defects the validator rejects (FR-015 acceptance scenarios):

- An unreachable state (no chain of transitions reaches it from the
  initial state).
- An ambiguous transition (duplicate transition ``key`` — v1 has no
  matching algorithm to pick one).
- A workflow with no terminal state at all (the case would never end).
- An invalid guard operator (anything outside the closed declarative
  rule operator set — Pillar P05 §2).
- A transition action referencing an unknown queue.
- A document-requirements guard whose ``classification`` is empty or
  not a string (P03-US5). The runtime also rejects a guard whose
  classification is not on the case type's ``document_classifications``
  allowlist at evaluation time.
- A non-terminating automatic cycle composed entirely of
  ``human_driven=False`` transitions.

A cycle containing at least one ``human_driven=True`` transition is
**allowed** — a human (or external event) can break the loop (plan §5:
"Human-driven workflow cycles are allowed").
"""

from __future__ import annotations

from collections.abc import Iterable

from app.application.configuration.validate_workflow_definition import (
    validate_definition as _validate_definition,
)
from app.domain.workflow import WorkflowVersion

__all__ = ["validate_definition"]


def validate_definition(
    version: WorkflowVersion,
    *,
    known_queues: Iterable[str] = (),
    known_classifications: Iterable[str] = (),
) -> None:
    """Validate a draft ``WorkflowVersion``. Raises on the first defect.

    Args:
        version: the draft workflow version under review.
        known_queues: the set of queue keys the workflow may reference
            in its transition ``actions``. v1 callers pass the queues
            the tenant has already configured; the validator is pure
            and does not look them up itself.
        known_classifications: the document classification labels the
            tenant has configured on its case types. Document guard
            ``classification`` values must lie in this set; an empty
            iterable disables the cross-check (the validator still
            enforces that the ``classification`` entry is a non-empty
            string when present, so a workflow cannot sneak through a
            malformed guard by relying on the cross-check being off).

    Raises:
        ValidationFailed: the definition has at least one defect.
    """
    _validate_definition(
        version,
        known_queues=known_queues,
        known_classifications=known_classifications,
    )
