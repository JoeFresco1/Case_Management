"""Cases HTTP router package (P01, P02 / FM-005..FM-007).

Routes are grouped by operation while sharing one router instance. Import
order preserves the former module's registration order. HTTP handlers
remain adapters over the application layer; authorization and tenant
scoping stay in their existing use cases and repositories.
"""

from __future__ import annotations

from . import create as _create  # noqa: F401
from . import edit as _edit  # noqa: F401
from . import read as _read  # noqa: F401
from . import transitions as _transitions  # noqa: F401
from .common import router
from .create import create_case
from .edit import edit_case
from .read import list_cases, view_case
from .transitions import execute_transition, list_transitions

__all__ = [
    "create_case",
    "edit_case",
    "execute_transition",
    "list_cases",
    "list_transitions",
    "router",
    "view_case",
]
