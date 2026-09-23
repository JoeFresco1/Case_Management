"""Application package: use cases, authorization, transaction boundaries.

The application layer depends only on `app.domain` and on protocols
(ports) defined in `app.domain.ports`. Adapters in `app.infrastructure`
are wired in at the HTTP boundary, not here.
"""

from __future__ import annotations

__all__: list[str] = []
