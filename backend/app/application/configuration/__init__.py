"""Configuration lifecycle use cases for case-type schemas (FM-008).

This module owns the validation and load paths for a published
``CaseTypeVersion``. The router layer calls into here; the application
layer never reaches into the repository directly so the validator can be
unit-tested with in-memory fakes.

Two entry points:

- :func:`validate_definition.validate_definition` — pure validation
  over a draft ``CaseTypeSchema``. Raises :class:`ValidationFailed` on
  the first defect so callers get a clear ``422``.
- :func:`load_case_type.load_for_create_case` — load the published
  schema for an incoming create-case request. Raises
  :class:`NotFound` if the key does not exist or has no published
  version.

The validator is the **publish gate** for FR-015 ("published versions
must be immutable"). Anything that survives validation is what every
``POST /cases`` request for that case-type key will see at runtime.
"""

from __future__ import annotations
