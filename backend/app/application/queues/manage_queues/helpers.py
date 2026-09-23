"""Shared helper: rewrite a task's assignee slots (claim / release / delegate).

One tiny module so :mod:`.claims` and :mod:`.delegations` share the
optimistic-concurrency contract without importing each other.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID


def _assign_task_to_user(
    task: Any,
    *,
    assignee_user_id: UUID | None,
    assignee_queue_id: str | None,
) -> Any:
    """Return a copy of ``task`` with the assignee slots rewritten.

    Uses ``dataclasses.replace`` to preserve the immutable contract;
    bumps ``version_token`` so the repository's optimistic check
    fires on any concurrent edit (Constitution V).
    """
    new_version = int(getattr(task, "version_token", 1)) + 1
    return replace(
        task,
        assignee_user_id=assignee_user_id,
        assignee_queue_id=assignee_queue_id,
        version_token=new_version,
    )
