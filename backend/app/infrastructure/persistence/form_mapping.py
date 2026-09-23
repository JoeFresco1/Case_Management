"""Lossless mapping between Forms domain values and portable ORM rows.

The canonical form definition is stored as JSON on ``form_versions``.
Keeping its serialization here prevents the shared persistence mapping module
from exceeding the repository's soft file-size limit and makes every E-2 field
property explicit for backward-compatible hydration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.form import (
    FormBinding,
    FormDefinition,
    FormFieldSpec,
    FormSectionSpec,
    FormVersion,
    FormVersionStatus,
)
from app.infrastructure.persistence.models import FormBinding as FormBindingRow
from app.infrastructure.persistence.models import FormVersion as FormVersionRow


def _field_to_json(field: FormFieldSpec) -> dict[str, Any]:
    """Serialize every declarative form-field property without loss."""
    return {
        "key": field.key,
        "label": field.label,
        "required": field.required,
        "placeholder": field.placeholder,
        "help_text": field.help_text,
        "field_type": field.field_type,
        "read_only": field.read_only,
        "tab_order": field.tab_order,
        "options": list(field.options),
        "min_length": field.min_length,
        "max_length": field.max_length,
        "minimum": field.minimum,
        "maximum": field.maximum,
        "pattern": field.pattern,
        "validation_message": field.validation_message,
        "default_value": field.default_value,
        "formula": dict(field.formula) if field.formula is not None else None,
        "lookup_source": field.lookup_source,
        "attachment_classification": field.attachment_classification,
        "attachment_max_size_bytes": field.attachment_max_size_bytes,
        "attachment_allowed_content_types": list(field.attachment_allowed_content_types),
        "signature_kind": field.signature_kind,
        "signature_connector": field.signature_connector,
        "system_property": field.system_property,
    }


def _field_from_json(raw: dict[str, Any]) -> FormFieldSpec:
    """Hydrate one field, defaulting properties absent from older snapshots."""
    return FormFieldSpec(
        key=raw["key"],
        label=raw.get("label", raw["key"]),
        required=raw.get("required", False),
        placeholder=raw.get("placeholder"),
        help_text=raw.get("help_text"),
        field_type=raw.get("field_type", "text"),
        read_only=raw.get("read_only", False),
        tab_order=raw.get("tab_order"),
        options=tuple(raw.get("options", ())),
        min_length=raw.get("min_length"),
        max_length=raw.get("max_length"),
        minimum=raw.get("minimum"),
        maximum=raw.get("maximum"),
        pattern=raw.get("pattern"),
        validation_message=raw.get("validation_message"),
        default_value=raw.get("default_value"),
        formula=raw.get("formula"),
        lookup_source=raw.get("lookup_source"),
        attachment_classification=raw.get("attachment_classification"),
        attachment_max_size_bytes=raw.get("attachment_max_size_bytes"),
        attachment_allowed_content_types=tuple(raw.get("attachment_allowed_content_types", ())),
        signature_kind=raw.get("signature_kind"),
        signature_connector=raw.get("signature_connector"),
        system_property=raw.get("system_property"),
    )


def form_version_to_domain(row: FormVersionRow) -> FormVersion:
    """Reconstruct a form version from its complete declarative JSON snapshot."""
    raw = row.definition_json or {}
    sections = tuple(
        FormSectionSpec(
            key=section["key"],
            label=section.get("label", section["key"]),
            fields=tuple(_field_from_json(field) for field in section.get("fields", ())),
            visible_when=section.get("visible_when"),
            kind=section.get("kind", "section"),
            description=section.get("description"),
            min_items=section.get("min_items"),
            max_items=section.get("max_items"),
        )
        for section in raw.get("sections", ())
    )
    return FormVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        version_number=row.version_number,
        definition=FormDefinition(
            sections=sections,
            validations=tuple(dict(rule) for rule in raw.get("validations", ())),
            steps=tuple(dict(step) for step in raw.get("steps", ())),
        ),
        status=FormVersionStatus(row.status),
        published_at=row.published_at,
        published_by=row.published_by,
        created_at=row.created_at,
    )


def form_version_from_domain(
    version: FormVersion, *, created_at: datetime | None = None
) -> dict[str, Any]:
    """Flatten a form version into values accepted by the portable ORM model."""
    created = created_at or version.created_at or datetime.now(UTC)
    return {
        "id": version.id,
        "tenant_id": version.tenant_id,
        "key": version.key,
        "version_number": version.version_number,
        "definition_json": {
            "sections": [
                {
                    "key": section.key,
                    "label": section.label,
                    "fields": [_field_to_json(field) for field in section.fields],
                    "visible_when": section.visible_when,
                    "kind": section.kind,
                    "description": section.description,
                    "min_items": section.min_items,
                    "max_items": section.max_items,
                }
                for section in version.definition.sections
            ],
            "validations": [dict(rule) for rule in version.definition.validations],
            "steps": [dict(step) for step in version.definition.steps],
        },
        "status": version.status.value,
        "published_at": version.published_at,
        "published_by": version.published_by,
        "created_at": created,
    }


def form_binding_to_domain(row: FormBindingRow) -> FormBinding:
    """Reconstruct a tenant-scoped form binding from its ORM row."""
    return FormBinding(
        id=row.id,
        tenant_id=row.tenant_id,
        case_type_version_id=row.case_type_version_id,
        form_version_id=row.form_version_id,
        published_at=row.published_at,
        published_by=row.published_by,
        created_at=row.created_at,
        state_forms=tuple(row.state_forms or ()),
        retired_at=row.retired_at,
        retired_by=row.retired_by,
    )


def form_binding_from_domain(binding: FormBinding) -> dict[str, Any]:
    """Flatten a form binding into values accepted by the portable ORM model."""
    return {
        "id": binding.id,
        "tenant_id": binding.tenant_id,
        "case_type_version_id": binding.case_type_version_id,
        "form_version_id": binding.form_version_id,
        "published_at": binding.published_at,
        "published_by": binding.published_by,
        "created_at": binding.created_at,
        "state_forms": list(binding.state_forms),
        "retired_at": binding.retired_at,
        "retired_by": binding.retired_by,
    }


__all__ = [
    "form_binding_from_domain",
    "form_binding_to_domain",
    "form_version_from_domain",
    "form_version_to_domain",
]
