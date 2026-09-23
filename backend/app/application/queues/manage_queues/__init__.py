"""Queue administration + worker claim/release flow (P02-US3, US9 / FM-016).

The use case surface for queue management:

- :func:`define_queue` — create a new queue (key, name, policy,
  members).
- :func:`reconfigure_queue` — rename, change description, switch
  policy / capacity, replace members.
- :func:`retire_queue` — remove a queue (cascade: open tasks
  assigned to it surface a domain error in the workflow engine).
- :func:`claim_next` — worker claims the next task from a queue
  (manual selection or round-robin).
- :func:`release_task` — worker returns a task to its queue (claim
  is reversible).
- :func:`bulk_handle` — worker claims / releases a batch of tasks
  in a single audit-logged request.
- :func:`delegate` — worker delegates their open tasks to another
  identity for a bounded period (P02-US9 / FM-016).
- :func:`expire_delegations` — system sweep that restores the prior
  owner once a delegation window passes (``now > until``). Tasks the
  delegate already completed or handed off are left alone (the
  delegation record is dropped as superseded).

# Why a package (R2 structural split)

Formerly a single ``manage_queues.py`` module of ~1050 lines — past the
R2 600-line soft limit. Split by responsibility into :mod:`ports`
(Protocols + audit subject constants), :mod:`commands` (frozen
command/result dataclasses), :mod:`admin` (define / reconfigure /
retire), :mod:`claims` (claim / release / bulk), :mod:`delegations`
(delegate / expire), and :mod:`helpers` (the shared assignee rewrite).
This ``__init__`` is a pure re-export shim: ``__all__`` and every
previously visible attribute are re-exported unchanged, so no consumer
import line needs editing and ``dir()`` stays identical.

# Authorization

All admin operations require ``ADMIN_TENANT_CONFIG``. The
worker-facing operations (``claim_next``, ``release_task``,
``bulk_handle``, ``delegate``) require ``TASK_READ`` on the queue
or case resource — the policy is evaluated against the case that
owns the task (cross-tenant actors are denied unconditionally).

# Tenant isolation

Every repository call is tenant-scoped; cross-tenant ids surface as
:class:`NotFound` so the API never confirms cross-tenant existence
(Constitution V). The test suite asserts this with two parallel
tenants; production mirrors the same ``WHERE tenant_id = :tenant_id``
guard.

# Audit + outbox atomicity

Each operation writes the queue / task change, an audit row, and
(outbound) domain events via the outbox in the same DB transaction.
The boundary commits once; failures roll back to a clean state so a
partial claim never lands.

# INTEGRATION_SEAM: TaskRepository

The stub ``SqlTaskRepository`` (T063/T071) ships in a follow-up
dispatch. The use case talks to the Protocol; the in-memory test
double substitutes until the SQL adapter lands.
"""

from __future__ import annotations

from dataclasses import dataclass, replace  # noqa: F401
from datetime import datetime  # noqa: F401
from typing import Any, Protocol  # noqa: F401
from uuid import UUID, uuid4  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.domain.audit import (  # noqa: F401
    QUEUE_DEFINED,
    QUEUE_RECONFIGURED,
    QUEUE_RETIRED,
    TASK_BULK_HANDLED,
    TASK_CLAIMED,
    TASK_DELEGATED,
    TASK_DELEGATION_EXPIRED,
    TASK_RELEASED,
)
from app.domain.context import UserContext  # noqa: F401
from app.domain.errors import Conflict, InvariantViolation, NotFound  # noqa: F401
from app.domain.queue import (  # noqa: F401
    AssignmentPolicy,
    Delegation,
    Queue,
    VisibilityScope,
)

from .admin import _require_admin, define_queue, reconfigure_queue, retire_queue  # noqa: F401
from .claims import bulk_handle, claim_next, release_task  # noqa: F401
from .commands import (  # noqa: F401
    BulkHandleCommand,
    BulkHandleResult,
    ClaimNextCommand,
    ClaimNextResult,
    DefineQueueCommand,
    DelegateCommand,
    ExpireDelegationsCommand,
    ExpireDelegationsResult,
    ReconfigureQueueCommand,
    ReleaseTaskCommand,
    RetireQueueCommand,
)
from .delegations import delegate, expire_delegations  # noqa: F401
from .helpers import _assign_task_to_user  # noqa: F401
from .ports import (  # noqa: F401
    SUBJECT_TYPE_QUEUE,
    SUBJECT_TYPE_TASK,
    _AuditStore,
    _CaseLookup,
    _Clock,
    _DelegationStore,
    _OutboxWriter,
    _QueueRepository,
    _TaskRepository,
)

__all__ = [
    "BulkHandleCommand",
    "BulkHandleResult",
    "ClaimNextCommand",
    "ClaimNextResult",
    "DefineQueueCommand",
    "DelegateCommand",
    "ExpireDelegationsCommand",
    "ExpireDelegationsResult",
    "ReconfigureQueueCommand",
    "ReleaseTaskCommand",
    "RetireQueueCommand",
    "claim_next",
    "define_queue",
    "delegate",
    "bulk_handle",
    "expire_delegations",
    "release_task",
    "reconfigure_queue",
    "retire_queue",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.queues.manage_queues)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (they are already in ``sys.modules``, which the
# import system consults before touching ``__path__``); only the redundant
# package-level attributes go away.
for _submodule in (
    "admin",
    "claims",
    "commands",
    "delegations",
    "helpers",
    "ports",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
