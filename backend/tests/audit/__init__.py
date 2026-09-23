"""Audit package for tests (FM-003, P09-US2/US3/US7).

The audit tests live in their own package so a future FM-046 (signed
batches) and FM-043 (timeline endpoint) can drop in alongside the
chain and immutability coverage without bloating the existing
``tests/application/cases`` or ``tests/security`` packages.
"""

from __future__ import annotations

__all__: list[str] = []
