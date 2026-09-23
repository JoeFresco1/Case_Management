"""Events router — ``GET /api/v1/events/catalog``.

P13-US8 / FM-055: external consumers (and internal UIs) need a
discoverable, documented list of event types the platform emits.
The catalog is the authoritative source — it is generated from
``app.domain.events.EVENT_CATALOG`` so a publisher cannot drift
from the documentation without a test failing.

# Authorization

The catalog is readable by any authenticated actor (no specific
role grant required). Authentication is still mandatory so anonymous
callers cannot enumerate the platform's event surface
(Constitution V). Tenant scope is implicit: the catalog itself is
global (event types are platform-wide), but the caller identity is
required to establish the tenant context for downstream
subscription calls.

# Pagination

The catalog is small and bounded, but the endpoint honors the
platform ``Page`` contract (``limit`` default 50, max 200) so
operator UIs can reuse the generic fetcher. ``next_cursor`` is
always ``None`` (single page); ``limit`` echoes the honored size.
``source`` is retained for back-compat with existing subscribers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status

from app.deps import UserContext, get_current_user
from app.domain.events import EVENT_CATALOG
from app.interfaces.http.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(tags=["events"])


@router.get(
    "/events/catalog",
    summary="List versioned domain event types",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
    },
)
async def list_event_catalog(
    user: UserContext = Depends(get_current_user),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum entries in the page (default 50, max 200)",
    ),
) -> dict[str, Any]:
    """Return the catalog of versioned domain event types.

    The ``payload_keys`` list is the contract subscribers negotiate
    against; removing a key requires a new ``event_type`` version
    (P13 Q2, ADR-023). ``limit`` truncates the catalog window;
    ``next_cursor`` is always ``None`` because the catalog fits in
    one page. ``user`` is unused beyond authentication — the catalog
    is global, but the identity requirement keeps enumeration
    authenticated and establishes tenant context for callers.
    """
    _ = user
    full = [
        {
            "event_type": spec.event_type,
            "version": spec.version,
            "description": spec.description,
            "payload_keys": list(spec.payload_keys),
        }
        for spec in EVENT_CATALOG
    ]
    window = full[:limit]
    return {
        "items": window,
        "next_cursor": None,
        "limit": limit,
        # Catalog is generated from the in-code EVENT_CATALOG tuple;
        # the field name documents that there is no external
        # registry yet. FM-047 adds a tenant-defined overlay via
        # GET /extensions/events/catalog.
        "source": "EVENT_CATALOG",
    }


__all__ = ["router"]
