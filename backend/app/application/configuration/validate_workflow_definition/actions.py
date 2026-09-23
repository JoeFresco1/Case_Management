"""Action validation — known action types, queue refs, SLA/escalation shape.

Every transition may declare ``actions`` the engine executes when the
transition fires. This module owns:

- the closed action-type allowlist :data:`_ALLOWED_ACTION_TYPES`
  (``create_task``, ``start_sla``, ``set_field``) so typos are
  rejected at publish instead of silently dropped at runtime;
- queue-reference checks against the caller-supplied ``known_queues``
  set (the validator stays pure — the publish use case looks queues up);
- the FM-018 ``start_sla`` deadline + escalation shape:
  :func:`_validate_start_sla_action`,
  :func:`_validate_escalation`, and the per-type helpers
  (:func:`_validate_supervisor_task_escalation`,
  :func:`_validate_queue_move_escalation`,
  :func:`_validate_exception_state_escalation`,
  :func:`_check_queue_known`).

Pure functions: no I/O. Split from the pre-R2 single-file validator
(``validate_workflow_definition.py``) by responsibility.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.domain.errors import ValidationFailed
from app.domain.workflow import WorkflowVersion

# Action types the workflow engine recognizes in v1. Anything else is
# rejected so we do not silently accept typos.
_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({"create_task", "start_sla", "set_field"})


def _validate_actions(
    version: WorkflowVersion,
    known_queues: Iterable[str],
) -> None:
    """Validate every action on every transition.

    Args:
        version: the draft workflow version under review.
        known_queues: queue keys the workflow may reference; an empty
            iterable skips queue-reference enforcement (unit tests).

    Raises:
        ValidationFailed: an action is not a JSON object, has an
            unknown ``type``, or references an unknown/ill-shaped
            queue or SLA fields.
    """
    queue_set = frozenset(known_queues)
    for transition in version.transitions:
        for idx, action in enumerate(transition.actions):
            path = f"transitions[{transition.key}].actions[{idx}]"
            if not isinstance(action, dict):
                raise ValidationFailed(
                    f"action must be a JSON object (got {type(action).__name__})",
                    field=path,
                )
            action_type = action.get("type")
            if action_type not in _ALLOWED_ACTION_TYPES:
                raise ValidationFailed(
                    f"unknown action type {action_type!r}",
                    field=f"{path}.type",
                )
            # ``create_task`` and ``start_sla`` may carry a queue
            # reference. We only enforce queue-reference shape when
            # the caller actually supplied a known_queues set (v1
            # callers always do at the publish gate).
            if action_type in {"create_task", "start_sla"} and queue_set:
                queue = action.get("queue")
                if queue is None or not isinstance(queue, str):
                    raise ValidationFailed(
                        f"{action_type!r} requires a string 'queue' reference",
                        field=f"{path}.queue",
                    )
                if queue not in queue_set:
                    raise ValidationFailed(
                        f"action references unknown queue {queue!r}",
                        field=f"{path}.queue",
                    )
            if action_type == "start_sla":
                _validate_start_sla_action(action, path=path, queue_set=queue_set)


def _validate_start_sla_action(
    action: dict[str, Any],
    *,
    path: str,
    queue_set: frozenset[str],
) -> None:
    """Validate a ``start_sla`` action's deadline + escalation shape (FM-018).

    - ``sla_key`` must be a non-empty string (the clock identity).
    - ``hours`` must be a positive number (the deadline offset).
    - an inline ``escalation`` / ``escalation_on_breach`` /
      ``escalation_on_warning`` dict must carry a known ``type``:

      - ``"create_task"`` — must name a non-empty ``queue``; when the
        caller supplied ``known_queues`` the queue must be known.
      - ``"supervisor_task"`` — supervisor follow-up task; ``queue``
        is optional (runtime defaults to ``"supervisor_queue"``);
        when present and ``known_queues`` was supplied it must be
        known.
      - ``"notify"`` — accepted as the deferred fan-out placeholder
        (documented in the FM-018 ADR — no transport exists, scanner
        reports ``escalation_fired=false``). No required fields.
      - ``"queue_move"`` — must name a ``target_queue`` (``"queue"``
        accepted as an alias); when ``known_queues`` was supplied it
        must be known. The scanner records the move as an auditable
        intent; the case-queue mutation itself is a later slice.
      - ``"exception_state"`` — must name an ``exception_state``
        (``"state"`` / ``"target_state"`` accepted as aliases). The
        scanner records the exception routing as an auditable
        intent; the workflow-instance move itself is a later slice.

    Args:
        action: the raw ``start_sla`` action mapping.
        path: dotted authoring path for ``ValidationFailed.field``.
        queue_set: known queue keys (empty skips the cross-check).

    Raises:
        ValidationFailed: ``sla_key``/``hours`` missing or invalid, or
            an escalation dict is malformed.
    """
    sla_key = action.get("sla_key")
    if not isinstance(sla_key, str) or not sla_key.strip():
        raise ValidationFailed(
            "'start_sla' requires a non-empty string 'sla_key'",
            field=f"{path}.sla_key",
        )
    raw_hours = action.get("hours")
    if raw_hours is None:
        raise ValidationFailed(
            "'start_sla' requires a numeric 'hours'",
            field=f"{path}.hours",
        ) from None
    try:
        hours = float(raw_hours)
    except (TypeError, ValueError):
        raise ValidationFailed(
            "'start_sla' requires a numeric 'hours'",
            field=f"{path}.hours",
        ) from None
    if hours <= 0:
        raise ValidationFailed(
            "'start_sla' requires 'hours' > 0",
            field=f"{path}.hours",
        )
    for esc_key in ("escalation", "escalation_on_breach", "escalation_on_warning"):
        esc = action.get(esc_key)
        if esc is None:
            continue
        if not isinstance(esc, dict):
            raise ValidationFailed(
                f"'start_sla.{esc_key}' must be a JSON object",
                field=f"{path}.{esc_key}",
            )
        _validate_escalation(esc, path=f"{path}.{esc_key}", queue_set=queue_set)


def _validate_escalation(
    esc: dict[str, Any],
    *,
    path: str,
    queue_set: frozenset[str],
) -> None:
    """Validate one inline escalation dict (FM-018 / T4-US3).

    Split from :func:`_validate_start_sla_action` so the deadline
    checks and the per-type escalation checks each stay within the
    branch budget. ``path`` is the dotted authoring path used in
    ``ValidationFailed.field`` (e.g. ``transitions[k].actions[0]``
    ``.escalation_on_breach``).

    Args:
        esc: the raw escalation mapping.
        path: dotted authoring path for ``ValidationFailed.field``.
        queue_set: known queue keys (empty skips the cross-check).

    Raises:
        ValidationFailed: unknown escalation ``type`` or a required
            field of the typed escalation is missing/ill-shaped.
    """
    esc_type = str(esc.get("type", "")).strip()
    if esc_type not in {
        "create_task",
        "notify",
        "supervisor_task",
        "queue_move",
        "exception_state",
    }:
        raise ValidationFailed(
            f"unknown escalation type {esc_type!r}",
            field=f"{path}.type",
        )
    if esc_type == "create_task":
        queue = esc.get("queue")
        if not isinstance(queue, str) or not queue.strip():
            raise ValidationFailed(
                "escalation 'create_task' requires a non-empty string 'queue'",
                field=f"{path}.queue",
            )
        _check_queue_known(queue, path=path, field=f"{path}.queue", queue_set=queue_set)
        return
    if esc_type == "supervisor_task":
        _validate_supervisor_task_escalation(esc, path=path, queue_set=queue_set)
        return
    if esc_type == "queue_move":
        _validate_queue_move_escalation(esc, path=path, queue_set=queue_set)
        return
    if esc_type == "exception_state":
        _validate_exception_state_escalation(esc, path=path)
        return
    # ``notify`` carries no required fields (deferred fan-out).


def _check_queue_known(queue: str, *, path: str, field: str, queue_set: frozenset[str]) -> None:
    """Reject an unknown queue reference when ``queue_set`` was supplied."""
    if queue_set and queue not in queue_set:
        raise ValidationFailed(
            f"escalation references unknown queue {queue!r}",
            field=field,
        )
    _ = path


def _validate_supervisor_task_escalation(
    esc: dict[str, Any], *, path: str, queue_set: frozenset[str]
) -> None:
    """Validate a ``supervisor_task`` escalation dict.

    Why the queue is optional: the scanner defaults a missing queue
    to ``"supervisor_queue"`` so authors can write the short form;
    a present queue is still validated when the caller supplied
    ``known_queues``.
    """
    queue = esc.get("queue")
    if queue is None:
        return
    if not isinstance(queue, str) or not queue.strip():
        raise ValidationFailed(
            "escalation 'supervisor_task' requires 'queue' to be a non-empty string when present",
            field=f"{path}.queue",
        )
    _check_queue_known(queue, path=path, field=f"{path}.queue", queue_set=queue_set)


def _validate_queue_move_escalation(
    esc: dict[str, Any], *, path: str, queue_set: frozenset[str]
) -> None:
    """Validate a ``queue_move`` escalation dict.

    Why two keys: ``target_queue`` is canonical, ``queue`` is the
    legacy alias authors already use on ``create_task``.
    """
    raw_target = esc.get("target_queue", esc.get("queue"))
    if not isinstance(raw_target, str) or not raw_target.strip():
        raise ValidationFailed(
            "escalation 'queue_move' requires a non-empty string 'target_queue' (alias 'queue')",
            field=f"{path}.target_queue",
        )
    _check_queue_known(raw_target, path=path, field=f"{path}.target_queue", queue_set=queue_set)


def _validate_exception_state_escalation(esc: dict[str, Any], *, path: str) -> None:
    """Validate an ``exception_state`` escalation dict.

    Why three keys: ``exception_state`` is canonical, ``state`` and
    ``target_state`` are authoring aliases.
    """
    raw_state = esc.get("exception_state", esc.get("state", esc.get("target_state")))
    if not isinstance(raw_state, str) or not raw_state.strip():
        raise ValidationFailed(
            "escalation 'exception_state' requires a non-empty string "
            "'exception_state' (aliases 'state', 'target_state')",
            field=f"{path}.exception_state",
        )
