"""Intake submission: create a case from a published case-bound form (T3-US1 / FM-036).

# Why a package (R2 structural split)

Formerly a single ``intake_submission.py`` module of ~940 lines — past
the R2 soft limit of 600. Split by responsibility into
:mod:`ports` (Protocol seams + subject/event constants),
:mod:`commands` (command/result dataclasses + actor proxy),
:mod:`field_processing` (deterministic field stages shared with
:mod:`app.application.forms.submit_form`), and :mod:`use_case` (the
async orchestration + persistence writes). This ``__init__`` is a pure
re-export shim: ``__all__`` and every previously visible attribute
(public *and* private) are re-exported unchanged, so no consumer
import line and no test ``patch`` target needs editing.

# What intake is

The intake use case is the ``submit a published case-bound form creates a case``
flow. Unlike :mod:`app.application.forms.submit_form` (which edits an existing
case), intake starts from no case row: the worker fills a published form bound
to a case-type version and the platform creates the ``Case`` aggregate, routes
attachments into the document pipeline, and records audit + outbox atomically.

Flow:

1. **Authorize** the actor against ``Action.CASE_CREATE`` on the target tenant
   (P11-US2 / FM-002). Cross-tenant actors are denied unconditionally.
2. **Resolve the published binding** for ``form_key`` plus the published
   ``FormVersion``. Draft / retired forms are rejected with
   ``ValidationFailed``. A binding whose tenant differs surfaces as
   ``Forbidden`` so the API never confirms cross-tenant configuration.
3. **Resolve the case-type version + case type** pinned by the binding. The
   case-type schema validates the final ``data`` payload (``Case.create``);
   the case-type key feeds the tenant reference template (FM-009).
4. **Evaluate section visibility** with empty stored data (intake has no prior
   case row) plus submission overrides for keys visibility rules reference.
5. **Drop hidden + read-only/system/calculated inbound values** (P07-US3).
   Caller-supplied values for computed keys never win; the platform owns them.
6. **Lookup pre-population**: blank ``lookup`` fields are filled from the
   configured ``LookupProvider`` before validation, so a worker who picks a
   primary value gets deterministic secondaries without hand-typing them.
7. **Calculated fields**: ``evaluate_calculated`` runs over the merged data in
   dependency order (iterated to a fixed point, bounded by field count). No
   arbitrary code: only the domain-whitelisted operators evaluate.
8. **System fields**: ``system`` properties (``case.reference``,
   ``actor.id`` ...) are filled from the intake context after the reference
   is issued.
9. **Deterministic validation**: visible required fields, per-field
   type/range/pattern/enum checks (``validate_field_value``), and form-level
   ``when -> then: required`` rules are evaluated together. All failures are
   collected into a single ``ValidationFailed`` whose ``field`` names every
   offending key, so the UI can highlight fields *without losing entered
   data* — the error is raised before any DB write, and the caller still
   holds the full ``submitted_data`` dict to re-render.
10. **Attachment routing**: each ``attachment`` field either references an
    already-stored document (``{"document_id": ...}`` verified against the
    same tenant) or carries an inline blob (``attachments[field_key]``)
    that is stored via ``DocumentStore.put`` *before* any row is staged.
    A storage failure raises ``DependencyUnavailable`` with nothing
    staged, so there is nothing to roll back (no partial commit).
11. **Signature seam**: ``handwritten`` / ``typed`` signatures are non-blank
    strings validated deterministically. ``external`` signatures delegate to
    the ``SignatureConnector`` port (a P14 connector placeholder); with no
    connector wired the field fails closed with ``ValidationFailed`` naming
    the field. No vendor SDK is imported here.
12. **Persist case + audit + outbox (+ documents) in one transaction**:
    ``case_repo.add``, one ``CaseCreated`` audit row carrying the form
    lineage, one ``CaseCreated`` outbox row, plus per-attachment
    ``DocumentAdded`` rows. The caller commits once; an audit/outbox failure
    rolls the case insert back.

Idempotency: the HTTP layer (``IdempotencyMiddleware``) caches the response
under ``Idempotency-Key`` and replays it without re-executing this use case,
so a retried intake never creates a duplicate case. The use case itself is
deterministic for identical inputs apart from fresh UUIDs / the monotonic
reference issued by ``CaseRepository.next_reference``.

# INTEGRATION_SEAM: LookupProvider / SignatureConnector / DocumentStore

- ``LookupProvider`` -> local configured-source adapter today; a future
  enterprise directory/registry adapter implements the same Protocol
  (declared in :mod:`.ports`).
- ``SignatureConnector`` -> P14 e-signature connector placeholder (no vendor
  SDK in domain/application per spec T3). ``None`` means "no external
  signer wired" and fails closed.
- ``DocumentStore`` -> ``LocalDocumentStore`` today; Azure Blob in
  production (``docs/integrations/seams.md``).
"""

from __future__ import annotations

# why origin imports here: the pre-split module imported these names at module
# scope, and dir() must stay byte-identical. Importing them from their origins
# (not via the submodules) also keeps mypy --strict happy — a submodule would
# need explicit ``X as X`` re-exports for every borrowed name.
import hashlib  # noqa: F401
from collections.abc import Mapping  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from typing import Any, Protocol  # noqa: F401
from uuid import UUID, uuid4  # noqa: F401

from app.application.authorization import Action, Resource, authorize  # noqa: F401
from app.application.cases.compute_derived import compute_derived_fields  # noqa: F401
from app.domain.audit import CASE_CREATED  # noqa: F401
from app.domain.case import Case  # noqa: F401
from app.domain.document import Document, DocumentSensitivity, DocumentVersion  # noqa: F401
from app.domain.errors import (  # noqa: F401 -- DependencyUnavailable re-exported for router mapping.
    DependencyUnavailable,
    Forbidden,
    InvariantViolation,
    NotFound,
    ValidationFailed,
)
from app.domain.form import (  # noqa: F401
    FormBinding,
    FormDefinition,
    FormVersion,
    FormVersionStatus,
    _evaluate_visibility,
    apply_submission_preservation,
    evaluate_calculated,
    read_only_field_keys,
    validate_field_value,
)
from app.domain.ports import (  # noqa: F401
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

from .commands import (  # noqa: F401
    AttachmentBlob,
    SubmitIntakeCommand,
    SubmitIntakeResult,
    _ActorProxy,
)
from .field_processing import (  # noqa: F401
    _is_blank,
    apply_calculated_fields,
    apply_lookup_prepopulation,
    apply_system_fields,
    check_signature_fields,
    field_map,
    hidden_field_keys,
    route_attachment_references,
    validate_visible_fields_deterministic,
    visible_field_keys,
)
from .ports import (  # noqa: F401
    DOCUMENT_ADDED,
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DOCUMENT,
    FormBindingRepository,
    FormVersionLoader,
    LookupProvider,
    SignatureConnector,
    _ClockLike,
)
from .use_case import (  # noqa: F401
    _check_blob_against_spec,
    _new_correlation_id,
    _replace_data,
    handle,
)

__all__ = [
    "AttachmentBlob",
    "DOCUMENT_ADDED",
    "FormBindingRepository",
    "FormVersionLoader",
    "LookupProvider",
    "SignatureConnector",
    "SubmitIntakeCommand",
    "SubmitIntakeResult",
    "apply_calculated_fields",
    "apply_lookup_prepopulation",
    "apply_system_fields",
    "check_signature_fields",
    "field_map",
    "handle",
    "hidden_field_keys",
    "route_attachment_references",
    "validate_visible_fields_deterministic",
    "visible_field_keys",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.forms.intake_submission)``
# byte-identical to the old module's attribute surface — the R2 refactor must
# be invisible. ``__path__`` is dropped for the same reason (a file module has
# no ``__path__``); every submodule is already in ``sys.modules`` after the
# imports above, so later ``import ...intake_submission.<sub>`` hits the
# module cache without consulting the package path. The submodules stay
# importable (``...intake_submission.use_case`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "commands",
    "field_processing",
    "ports",
    "use_case",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
