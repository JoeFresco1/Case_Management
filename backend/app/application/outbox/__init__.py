"""Admin outbox application package.

The package owns the HTTP composition contracts for the operator
outbox console (``GET /api/v1/admin/outbox``,
``POST /api/v1/admin/outbox/{id}/replay``). It depends only on the
domain layer (``app.domain``) and on Protocols declared here. The
concrete dispatcher and SQL adapters in ``app.infrastructure`` are
wired in at the HTTP boundary.
"""

from __future__ import annotations

__all__: list[str] = []
