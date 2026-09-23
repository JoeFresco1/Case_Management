"""Application package marker for ``app.application.extensions``.

Kept as an empty package so the ``app.application.extensions`` import
path resolves in tests and routers. The actual use case lives in
``register_extension.py`` to keep this module's footprint tiny (AGENTS
.md §4.R2 — small files).
"""

from __future__ import annotations

__all__: list[str] = []
