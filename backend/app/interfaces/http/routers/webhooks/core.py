"""Shared ``APIRouter`` instance for the webhooks HTTP surface (R2 split).

Why this module exists (R2 structural split): the former single-file
``webhooks.py`` module grew to 1030 lines — past the R2 1000-line hard
cap. The inbound receiver route (:mod:`receive`) and the outbound
subscription management routes (:mod:`subscriptions`) both register
onto the ONE ``router`` defined here, so ``app.main`` still imports a
single ``router`` from the package — the consumer contract is
unchanged.

This module also owns the route-precedence fix
(:func:`_static_routes_first`), which the package ``__init__`` calls
exactly once after every route module has registered its routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["webhooks"])

# ---------------------------------------------------------------------------
# Route precedence (Starlette matches in registration order)
# ---------------------------------------------------------------------------
#
# Why this block exists: the inbound ``POST /webhooks/{channel}``
# catch-all is *defined* before the outbound subscription routes, so
# without intervention ``POST /webhooks/subscriptions`` would match
# with ``channel="subscriptions"`` and fail HMAC verification instead
# of reaching the subscription contract (FM-056). Reorder once at
# import time — the package ``__init__`` calls this after both route
# modules have registered — so every static ``/webhooks/subscriptions...``
# route precedes the templated channel route, preserving definition
# order within each group. ``include_router`` clones this order, so both
# the production app and minimal test apps observe the same
# precedence.


def _static_routes_first() -> None:
    """Sort static subscription routes ahead of the channel catch-all.

    Rewrites ``router.routes`` in place: routes whose path contains no
    ``{channel}`` placeholder sort first, the templated channel route
    sorts last, and the original registration order is preserved
    within each group (the sort key falls back to the route's index).
    """
    router.routes[:] = sorted(
        router.routes,
        key=lambda route: (
            0 if "{channel}" not in getattr(route, "path", "") else 1,
            router.routes.index(route),
        ),
    )
