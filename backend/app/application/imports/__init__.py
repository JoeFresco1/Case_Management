"""Imports application package.

The package owns the import pipeline use cases (P14-US2, US3, US4 /
P04-US3). It depends only on the domain layer (``app.domain``) and on
Protocols declared in ``app.domain.ports``. Adapters in
``app.infrastructure.persistence`` are wired in at the HTTP boundary.

Why a dedicated package: the import pipeline is the canonical capture
channel for bulk data. It is the only path that creates many cases in
one transaction. Separating it from the single-case ``application/cases``
package keeps the per-case use case small and the bulk path easy to
audit.
"""

from __future__ import annotations

__all__: list[str] = []
