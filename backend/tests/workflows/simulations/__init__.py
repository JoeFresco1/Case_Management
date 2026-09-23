"""Synthetic-case fixtures for the workflow simulator (FM-015 / P02-US5).

These JSON fixtures pair a synthetic case with an optional ordered list
of actor actions. The simulator consumes both and produces a
deterministic ``SimulationResult``. The fixtures live next to the
test file (``backend/tests/workflows/simulations/``) so an admin UI
maintainer can read the same JSON the test asserts against.

Each fixture is a single object:

- ``name`` -- human label, surfaces in test ids.
- ``synthetic_case`` -- the case-shaped mapping the guards read.
- ``actions`` -- ordered list of actor decisions. Each entry has a
  ``transition_key`` (must match a declared transition on the workflow
  under test) and a ``payload`` (echoed back, not interpreted).
- ``expected`` -- the assertions the test will check:
  - ``outcome`` -- ``completed`` / ``blocked`` / ``exception``.
  - ``final_state`` -- the state key the simulation stops in.
  - ``step_count`` -- the number of steps the simulator fires.
  - ``first_transition`` / ``last_transition`` -- convenient anchors
    for assertions on the trace.

Fixtures that target the seed workflow (``ServiceRequestWorkflow``)
carry ``workflow_key: "ServiceRequestWorkflow"``. The test loads the
matching ``WorkflowVersion`` from the in-memory fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent


def load_fixture(name: str) -> dict[str, Any]:
    """Load one fixture JSON file by its stem name."""
    path = _FIXTURES_DIR / f"{name}.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def list_fixtures() -> list[str]:
    """Return every fixture stem (without ``.json``) in this directory."""
    return sorted(p.stem for p in _FIXTURES_DIR.glob("*.json"))


__all__ = ["list_fixtures", "load_fixture"]
