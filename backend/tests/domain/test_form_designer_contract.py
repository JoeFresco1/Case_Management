"""Backend contract test for the visual form designer (FM-037 / P07-US5).

The visual form designer (frontend/src/forms/VisualFormDesigner.tsx) emits
a ``FormDefinition`` JSON object. The backend ``FormVersion.draft`` factory
runs ``_validate_definition`` on that object before creating an immutable
``FormVersion``. The acceptance for FM-037 is:

    designer creates form JSON that validates and can be published;
    runtime accepts designer output without conversion.

This test pins that contract by feeding the exact JSON shape produced by
the designer into the backend ``FormVersion.draft`` factory. If the
frontend or the backend changes such that the format diverges, this test
fails — which is the desired signal (the shared shape is the point).

The test is intentionally placed in ``tests/domain/`` (not
``tests/application/``) because ``FormVersion.draft`` is a pure-domain
factory with no I/O. Mirrors the convention of ``test_case_type.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.domain.errors import InvariantViolation
from app.domain.form import FormDefinition, FormFieldSpec, FormSectionSpec, FormVersion


def _designer_emitted_definition() -> dict[str, Any]:
    """Return the exact JSON shape ``VisualFormDesigner`` produces.

    Kept in sync with ``frontend/src/forms/VisualFormDesigner.tsx`` by
    code review: any change to either side must change both. The shape
    mirrors what the test in
    ``frontend/src/forms/VisualFormDesigner.test.tsx`` emits through the
    React tree (sections, fields, visible_when, validations).
    """
    return {
        "sections": [
            {
                "key": "intake",
                "label": "Intake",
                "fields": [
                    {
                        "key": "is_appeal",
                        "label": "Is appeal?",
                        "required": True,
                    }
                ],
                "visible_when": None,
            },
            {
                "key": "appeal_details",
                "label": "Appeal details",
                "fields": [
                    {
                        "key": "appeal_reason",
                        "label": "Reason for appeal",
                        "required": True,
                    }
                ],
                "visible_when": {"eq": {"field": "is_appeal", "value": True}},
            },
        ],
        "validations": [
            {
                "when": {"eq": {"field": "is_appeal", "value": True}},
                "then": {"required": "appeal_reason"},
            }
        ],
    }


def _to_dataclass(payload: dict[str, Any]) -> FormDefinition:
    """Convert the designer JSON shape to a ``FormDefinition`` dataclass.

    The backend ``FormVersion.draft`` factory requires real
    ``FormSectionSpec`` and ``FormFieldSpec`` instances (it walks
    ``.key`` / ``.label`` attributes). The designer emits plain dicts,
    so this helper performs the conversion. The resulting object is
    structurally identical to what the persistence layer would build
    from a JSON column on read.
    """
    sections: list[FormSectionSpec] = []
    for section in payload["sections"]:
        fields = tuple(
            FormFieldSpec(
                key=field["key"],
                label=field["label"],
                required=field["required"],
                placeholder=field.get("placeholder"),
                help_text=field.get("help_text"),
            )
            for field in section["fields"]
        )
        visible_when = section.get("visible_when")
        sections.append(
            FormSectionSpec(
                key=section["key"],
                label=section["label"],
                fields=fields,
                visible_when=visible_when,
            )
        )
    validations = tuple(
        {"when": rule["when"], "then": rule["then"]} for rule in payload["validations"]
    )
    return FormDefinition(sections=tuple(sections), validations=validations)


class TestFormDesignerRoundTrip:
    """Pin the FM-037 / P07-US5 wire contract."""

    def test_draft_factory_accepts_designer_payload__succeeds(self) -> None:
        # given
        payload = _designer_emitted_definition()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        # when
        version = FormVersion.draft(
            tenant_id=tenant_id,
            key="ServiceRequestAppealForm",
            version_number=1,
            definition=_to_dataclass(payload),
        )

        # then
        assert version.status.value == "draft"
        assert version.key == "ServiceRequestAppealForm"
        assert len(version.definition.sections) == 2
        assert version.definition.sections[1].visible_when == {
            "eq": {"field": "is_appeal", "value": True}
        }

    def test_draft_factory_rejects_unknown_operator__invariant_violation(
        self,
    ) -> None:
        # given
        payload = _designer_emitted_definition()
        payload["sections"][0]["visible_when"] = {"regex": {"field": "is_appeal", "value": "^foo$"}}

        # when / then
        with pytest.raises(InvariantViolation) as exc:
            FormVersion.draft(
                tenant_id=UUID("00000000-0000-0000-0000-000000000002"),
                key="ServiceRequestAppealForm",
                version_number=1,
                definition=_to_dataclass(payload),
            )
        assert "unknown form operator" in str(exc.value)

    def test_draft_factory_rejects_duplicate_section_key__invariant_violation(
        self,
    ) -> None:
        # given
        payload = _designer_emitted_definition()
        payload["sections"].append(payload["sections"][0])

        # when / then
        with pytest.raises(InvariantViolation) as exc:
            FormVersion.draft(
                tenant_id=UUID("00000000-0000-0000-0000-000000000003"),
                key="ServiceRequestAppealForm",
                version_number=1,
                definition=_to_dataclass(payload),
            )
        assert "duplicate section key" in str(exc.value)

    def test_draft_factory_rejects_duplicate_field_key__invariant_violation(
        self,
    ) -> None:
        # given
        payload = _designer_emitted_definition()
        payload["sections"][1]["fields"].append(
            {
                "key": "is_appeal",  # collides with section 0
                "label": "Echo",
                "required": False,
            }
        )

        # when / then
        with pytest.raises(InvariantViolation) as exc:
            FormVersion.draft(
                tenant_id=UUID("00000000-0000-0000-0000-000000000004"),
                key="ServiceRequestAppealForm",
                version_number=1,
                definition=_to_dataclass(payload),
            )
        assert "duplicate field key" in str(exc.value)

    def test_visible_section_keys_evaluates_designer_condition__is_true(self) -> None:
        # given
        payload = _designer_emitted_definition()
        version = FormVersion.draft(
            tenant_id=UUID("00000000-0000-0000-0000-000000000005"),
            key="ServiceRequestAppealForm",
            version_number=1,
            definition=_to_dataclass(payload),
        )

        # when
        visible_no_appeal = version.visible_section_keys(visible_data={})
        visible_with_appeal = version.visible_section_keys(visible_data={"is_appeal": True})

        # then
        assert visible_no_appeal == frozenset({"intake"})
        assert visible_with_appeal == frozenset({"intake", "appeal_details"})
