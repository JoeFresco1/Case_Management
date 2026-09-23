# Pillar P05 — Metadata & Indexing

**Status**: Draft · **Owner**: Platform team · **Updated**: 2026-08-31

> Structured and unstructured metadata is defined, validated, and made queryable in a controlled way.

## 1. Pillar statement

This pillar owns the **definition** of metadata: case schemas, document classifications, field types, validation, searchability, sensitivity, indexing. The pillar is configuration-heavy and code-light; it produces the rules other pillars enforce.

A field has a stable machine key, a label, a type, optional validation, optional sensitivity, and a flag indicating whether it is searchable, sortable, exportable, and PII-bearing. Fields are never free-form: every field on a case is defined in a published Case Type Version (P01) and validated at write time.

The pillar also owns **indexing strategy** for queryable fields — which fields are indexed, which are full-text-searched, which are stored only for display. Indexing is configurable; search lives in P06.

## 2. Scope

### In scope
- Field types: text, longtext, integer, decimal, boolean, date, datetime, enum, multichoice, reference, json.
- Requiredness, sensitivity, searchability, exportability, PII flags per field.
- Per-field validation rules (declarative; no arbitrary code in v1).
- Case Type schemas as versioned, immutable configuration.
- Document classification taxonomy.
- Field key immutability across versions (renaming a label does not break history).
- Indexing rules: which fields are indexed, how, with what cardinality.

### Out of scope (deferred)
- Computed/derived fields that require code (P3; declarative-only in v1).
- A UI for case type design (P3; first slice ships JSON).
- Multi-language labels (P3).
- Field-level encryption (P3; P11 owns the seam).

### Explicitly not building
- A "data dictionary" that doubles as a code-generation tool. The pillar defines schemas; code follows the schemas, not the other way around.
- A "free text" case type. Every field has a type.

## 3. Primary entities

| Entity | Purpose | First slice |
|---|---|---|
| `FieldDefinition` | A single field in a schema. | Yes |
| `CaseTypeSchema` | The set of fields for a CaseType Version. | Yes |
| `FieldValidation` | A declarative rule on a field (e.g., enum values, range). | Yes |
| `FieldIndexHint` | Indicates how a field should be indexed. | Deferred |
| `DocumentClassification` | A label applied to documents. | Yes (basic) |
| `SensitivityLabel` | A sensitivity level (public / internal / confidential / restricted / phi). | Yes |

## 4. User stories (priority-tiered)

### P1 — Critical
- **P05-US1**: As a process administrator, I can define a Case Type schema with named fields, types, requiredness, and validation; the schema is published as a versioned artifact; existing cases retain the version they were created with.
- **P05-US2**: As a case worker, when I submit a case, the system validates each field against the published schema; missing required fields and type-mismatched values are rejected with structured errors.

### P2 — Important
- **P05-US3**: As a process administrator, I can mark a field as sensitive (e.g., `phi: true`); the field is redacted in logs, events, and search results for actors without permission.
- **P05-US4**: As a process administrator, I can mark a field as searchable; the field is included in the case search index and is queryable by authorized actors.
- **P05-US5**: As a process administrator, I can define a document classification taxonomy (e.g., "Contract", "LabResult", "Correspondence"); classifications are enforced on document upload.

### P3 — Deferred
- **P05-US6**: As a process administrator, I can define a derived field (e.g., `total = quantity * unit_price`) in a constrained declarative language.
- **P05-US7**: As a process administrator, I can mark a field as "encrypted at rest" and the system uses the field-level encryption seam.
- **P05-US8**: As an administrator, I can rename a field label across all case types and the platform records the rename in the field's history.

## 5. Cross-cuts

- **P01 (Case)**: a Case's data is validated against the schema owned by this pillar.
- **P03 (Document)**: a Document's classification is owned by this pillar.
- **P06 (Search)**: searchability flags flow to the search index.
- **P09 (Audit)**: schema changes are audited; field-level changes are audited.
- **P10 (Administration)**: schemas are configuration artifacts; lifecycle owned by P10.
- **P11 (Security)**: sensitivity labels drive authorization and redaction.

## 6. Acceptance scenarios (representative)

- E1: A Case Type with three required fields is published. Submitting a case missing one field is rejected with `validation_failed` and the missing field name.
- E2: A Case Type Version is published. An attempt to mutate it is rejected. A new version is created instead.
- E3: A field is marked `phi: true`. A search result for that field value is returned with the value redacted; the underlying case's data is unaffected.
- E4: A field's label is changed in a new version. Existing cases retain the old label internally; the UI shows the new label going forward; integrations that reference the field by machine key are unaffected.

## 7. Planned slices

| Slice | Status | Notes |
|---|---|---|
| `001-case-management-platform` | Active | First slice. Implements P05-US1, US2, US5 (basic). Sensitivity labels and searchability flags are partial. |
| `002-leftovers` Run A | Draft | Sensitivity labels drive the redaction boundary (FM-011 / P05-US3): `sensitive_field_keys()` feeds the pure redactor. See `specs/002-leftovers/architecture-runA.md`, `docs/decisions/ADR-037-runA-redaction-boundary.md`. |
| `002-leftovers` Run B | Draft | Searchable/sensitive key sets become the sealed P05→P06 contract: the router unions them from published schemas and hands them to search; non-searchable removal takes effect immediately (LIKE needs no reindex). See `specs/002-leftovers/architecture-runB.md §2`. |
| Future slice 2 | Planned | Sensitivity-driven redaction (US3), search index integration (US4). |
| Future slice 3 | Planned | Derived fields (US6), field-level encryption (US7). |

## 8. Open questions

- Q1: How are enum values versioned when they change? Current intent: enums are part of the schema version; an enum value cannot be removed if any case uses it.
- Q2: When a field's type changes incompatibly (e.g., text → integer), is automatic data migration attempted? (Current intent: no — the field is treated as a new field; the old field is preserved as historical data under the old version.)
- Q3: For a `reference` field, what entities are referencable? (Current intent: participants, cases, documents; P3 may extend.)
