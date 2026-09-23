"""Tests for the AI capability boundary (FM-050, P11-US12).

Acceptance targets (from the task spec):

- AI recommendation logged with confidence, source, and human decision
  kept separate.
- Workflow transition never auto-authorized by AI.
- Deterministic core passes without an LLM.

# Why these tests live under ``tests/ai/`` instead of
# ``tests/application/ai/``

The task spec points explicitly at ``backend/tests/ai/``. The
directory is also referenced by FM-050's "tests for the boundary
live next to the seam" note in ADR-012. Keeping the directory flat
means future readers grep ``tests/ai`` and find every test that
exercises the seam.

# Layout

The suite mirrors the structure of the seam:

- ``test_noop_stub.py`` — the local default adapter; deterministic
    guarantees, confidence shape, source_refs shape.
- ``test_boundary_protocol.py`` — the ``AIAdvisor`` Protocol and the
    request/recommendation dataclasses; invariants on the shape.
- ``test_human_decision.py`` — ``HumanDecision`` separates the human
    from the AI; the validation rules; the seam against the system
    actor.
- ``test_workflow_authorization_unchanged.py`` — proves the
    workflow authorization path does NOT depend on AI at all
    (FM-050 acceptance: workflow transition never auto-authorized by
    AI, and the deterministic core works without an LLM).
"""
