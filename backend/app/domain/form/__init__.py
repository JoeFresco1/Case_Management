"""FormDefinition, FormVersion, and FormBinding aggregates (P07, FM-036/037/038).

Forms are declarative versioned specs bound to a ``CaseTypeVersion``. They carry
sections/panels/repeating groups, deterministic field types, conditional visibility,
form-level validation, explicit tab order, calculated/lookup/attachment/signature/
system fields. Published snapshots are immutable (FR-015 G3); cases pin versions
through ``FormBinding`` (G4). No arbitrary code: whitelisted operators/formulas only
(Constitution IX, XIV). Hidden/read-only/system values are preserved on submit (US3).

# Why a package (R2 structural split)

Formerly a single ``form.py`` module of ~1140 lines — past the R2 600-line
soft limit. Split by responsibility into :mod:`constants` (whitelists),
:mod:`helpers` (shared key/scalar guards), :mod:`specs` (field/section/
definition dataclasses + layout queries), :mod:`conditions` (condition DSL
check + evaluate), :mod:`validation` (draft/publish definition validation),
:mod:`version` (FormVersion/FormBinding lifecycles), :mod:`values`
(submitted-value validation + preservation merges), and :mod:`calculated`
(deterministic formula evaluation). This ``__init__`` is a pure re-export
shim: ``__all__`` and every previously visible attribute — including the
private ``_helpers`` — are re-exported unchanged, so no consumer import line
and no ``mock.patch("app.domain.form.…")`` target needs editing.

# INTEGRATION_SEAM: repositories are domain ports; local adapter under
# ``app/infrastructure/persistence/repositories/``; Azure behind same Protocol.
# Attachments route to ``DocumentStore``, lookups to a configured source adapter,
# external signatures to a P14 connector — none imported here.
"""

from __future__ import annotations

from .calculated import (  # noqa: F401
    _as_number,
    _evaluate_arithmetic,
    _evaluate_if_present,
    _first_present,
    evaluate_calculated,
)
from .conditions import (  # noqa: F401
    _check_comparison_operand,
    _check_condition,
    _check_exists_operand,
    _check_truthy_operand,
    _collect_referenced,
    _evaluate_visibility,
    _section_visibility_referenced_keys,
)
from .constants import (  # noqa: F401
    _ALLOWED_OPERATORS,
    _ARITHMETIC_OPERATORS,
    _CALCULATED_OPERATORS,
    _CODE_LIKE_KEYS,
    _CONNECTOR_RE,
    _FIELD_KEY_RE,
    _FIELD_TYPES,
    _MAX_CONDITION_DEPTH,
    _NUMERIC_FIELD_TYPES,
    _SECTION_KINDS,
    _SIGNATURE_KINDS,
    _SYSTEM_PROPERTIES,
    _TEXT_LIKE_FIELD_TYPES,
    re,
)
from .helpers import (  # noqa: F401
    _formula_operands,
    _is_json_scalar,
    _reject_code_like_key,
    _require_field_key,
)
from .specs import (  # noqa: F401
    Any,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    Mapping,
    _check_default_matches_type,
    _check_formula_operand_shape,
    _check_formula_shape,
    _field_map,
    _require_operand_keys,
    dataclass,
    effective_tab_order,
    read_only_field_keys,
)
from .validation import (  # noqa: F401
    InvariantViolation,
    _check_calculated_refs,
    _check_section_visibility_refs,
    _check_validation_rule,
    _index_definition,
    _reject_duplicate_tab_order,
    _reject_section_visibility_cycle,
    _validate_definition,
    _validate_steps,
)
from .values import (  # noqa: F401
    ValidationFailed,
    _validate_boolean_value,
    _validate_decimal_value,
    _validate_integer_value,
    _validate_multichoice_value,
    _validate_nonempty_string,
    _validate_text_value,
    apply_hidden_field_preservation,
    apply_submission_preservation,
    validate_field_value,
)
from .version import (  # noqa: F401
    UUID,
    FormBinding,
    FormVersion,
    FormVersionStatus,
    StrEnum,
    datetime,
    uuid4,
)

__all__: list[str] = [
    "FormDefinition",
    "FormFieldSpec",
    "FormSectionSpec",
    "FormVersion",
    "FormVersionStatus",
    "FormBinding",
    "apply_hidden_field_preservation",
    "apply_submission_preservation",
    "effective_tab_order",
    "evaluate_calculated",
    "read_only_field_keys",
    "validate_field_value",
    "_evaluate_visibility",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages also expose ``__path__`` — neither existed on
# the pre-split single-file module. Popping them keeps
# ``dir(app.domain.form)`` byte-identical to the old module's attribute
# surface: the R2 refactor must be invisible, and the dir() gate diffs names
# exactly. Every submodule is eagerly imported above, so later resolves
# (``from app.domain.form import X``, ``mock.patch("app.domain.form.X)``,
# ``import app.domain.form.<sub>``) all hit ``sys.modules`` directly and never
# need the package-level attributes back.
for _submodule in (
    "__path__",
    "calculated",
    "conditions",
    "constants",
    "helpers",
    "specs",
    "validation",
    "values",
    "version",
):
    globals().pop(_submodule, None)
del _submodule
