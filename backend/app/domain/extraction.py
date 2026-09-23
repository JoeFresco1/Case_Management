"""Assisted-indexing rules: barcode, OCR, template zones, ICR, AutoFill (T1).

This module is the **pure-domain** kernel for the assisted-indexing
paragraph of ``specs/002-leftovers/spec.md`` §3.T1:

- *"Assisted indexing: barcode-driven type/field assignment with
  separator-page splitting; OCR text extraction (full-page + zonal +
  point-and-shoot); template-zone extraction (form ID zone + data
  zones); handwriting recognition as optional adapter; pre-population
  sets (primary -> secondaries)."*
- Migration sources: ``Bar Code Process, Full-Page OCR, Advanced Capture
  Form Zones, ICR, AutoFill Keyword Sets`` — business terminology only
  (Constitution XII); legacy names appear here and nowhere else.

# Deterministic core, bounded assistance

Every function here is pure: same inputs produce the same suggestion,
with no I/O and no vendor SDK (acceptance: *"no vendor SDK in
domain"*). Vendor engines (barcode libraries, OCR/ICR engines, form
recognizers) live behind the Protocols in
:mod:`app.domain.ports.extraction`; this module only owns what the
platform decides about their *outputs*:

- which barcode value maps to which case type / field assignment,
- where separator pages split a batch into documents,
- when an OCR confidence is too low to trust (``needs_review``),
- whether a template's form-ID zone matches (mismatch quarantines),
- which secondaries an AutoFill primary implies.

Suggestions are **bounded assistance** (Constitution XIV): every result
carries ``needs_human_verification=True`` and a confidence + source
context. Nothing here files a case, routes work, or disposes a record —
the human verification recorded on :class:`ScanBatch
<app.domain.scan_batch.ScanBatch>` remains authoritative.

# INTEGRATION_SEAM: extraction adapters

Future production adapters (barcode engine, Azure AI Vision OCR, Form
Recognizer template zones, ICR handwriting) implement the ports and are
wired via ``app/config.py`` + ``app/deps.py``. Tests use the fakes in
``tests/domain/test_extraction.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from app.domain.errors import ValidationFailed

# Below this confidence an extraction output must be reviewed by a
# human before it may drive indexing. The threshold is a platform
# constant (not per-adapter) so operator expectations are uniform
# regardless of which engine produced the score.
MIN_TRUSTED_CONFIDENCE: Final[float] = 0.80

# Maximum zones per template. A form with more data zones than this is
# a multi-form packet the operator should model as several templates,
# not one sprawling zone map (keeps the audit payload bounded).
MAX_TEMPLATE_ZONES: Final[int] = 100

# Maximum secondaries per AutoFill primary. Same rationale: a primary
# that fans out wider is a lookup-table concern (T3 lookups), not an
# inline pre-population set.
MAX_AUTOFILL_SECONDARIES: Final[int] = 50

_SEPARATOR_BARCODE_KINDS: Final[frozenset[str]] = frozenset({"separator", "batch_sep"})


@dataclass(frozen=True, slots=True)
class Barcode:
    """One decoded barcode value from a scanned page.

    The domain never decodes pixels — the ``BarcodeDecoder`` adapter
    does. This shape is what the adapter reports: the symbology (e.g.
    ``"code128"``, ``"qr"``), the decoded value, the 1-based page it
    was found on, and the adapter's confidence.
    """

    symbology: str
    value: str
    page_number: int
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.symbology:
            raise ValidationFailed("barcode requires a symbology", field="symbology")
        if not self.value:
            raise ValidationFailed("barcode requires a value", field="value")
        if self.page_number < 1:
            raise ValidationFailed(
                f"barcode page_number must be >= 1, got {self.page_number}",
                field="page_number",
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed(
                f"barcode confidence must be 0..1, got {self.confidence}",
                field="confidence",
            )

    @property
    def is_separator(self) -> bool:
        """Return True iff this barcode marks a document boundary.

        Separator pages are barcodes whose *value* carries the
        ``SEP:`` prefix or whose symbology is a registered separator
        kind. The prefix convention keeps the rule data-driven: the
        operator prints ``SEP:<doc-type>`` stickers without changing
        platform code.
        """
        return self.symbology in _SEPARATOR_BARCODE_KINDS or self.value.startswith("SEP:")


@dataclass(frozen=True, slots=True)
class BarcodeAssignment:
    """The indexing the platform derives from one barcode.

    ``case_type_key`` is the case type to file under; ``field_values``
    are the pre-populated field assignments (stringly typed — the
    case-type schema validates them at write time). ``needs_review``
    is set whenever the barcode confidence is below
    ``MIN_TRUSTED_CONFIDENCE`` so the operator confirms a shaky read
    before it drives routing.
    """

    case_type_key: str
    field_values: dict[str, str]
    confidence: float
    needs_review: bool


def assign_from_barcode(
    *,
    barcode: Barcode,
    mapping: dict[str, dict[str, str]],
    case_type_key: str,
) -> BarcodeAssignment:
    """Map a barcode value to field assignments via a versioned mapping.

    Args:
        barcode: The decoded barcode to index from.
        mapping: The published barcode mapping (``value ->
            {field_key: field_value}``). Versioned alongside the case
            type; the caller supplies the active version's rows.
        case_type_key: The case type the channel routes this batch to.

    Returns:
        The assignment. Unknown barcode values produce an *empty*
        assignment with ``needs_review=True`` (the page is indexed by
        hand, never dropped silently).

    Raises:
        ValidationFailed: On an empty ``case_type_key``.
    """
    if not case_type_key:
        raise ValidationFailed("barcode assignment requires a case_type_key", field="case_type_key")
    fields = mapping.get(barcode.value)
    if fields is None:
        # Why empty + review instead of raising: an unmapped barcode is
        # an operational signal (new sticker roll, misprint), not a
        # construction bug. The operator indexes the page by hand and
        # the mapping admin adds the value for next time.
        return BarcodeAssignment(
            case_type_key=case_type_key,
            field_values={},
            confidence=barcode.confidence,
            needs_review=True,
        )
    return BarcodeAssignment(
        case_type_key=case_type_key,
        field_values=dict(fields),
        confidence=barcode.confidence,
        needs_review=barcode.confidence < MIN_TRUSTED_CONFIDENCE,
    )


def split_on_separator_pages(
    *,
    page_count: int,
    barcodes: tuple[Barcode, ...],
) -> tuple[tuple[int, int], ...]:
    """Split batch pages into document ranges at separator barcodes.

    Args:
        page_count: Total pages in the batch (>= 1).
        barcodes: Decoded barcodes; only ``is_separator`` entries act
            as boundaries. The separator page itself starts the new
            document (it is the cover sheet of what follows).

    Returns:
        One ``(start_page, end_page)`` 1-based inclusive range per
        document, in ascending order, fully partitioning ``1..page_count``.

    Raises:
        ValidationFailed: On a non-positive page count, an out-of-range
            separator, or duplicate separators on one page.
    """
    if page_count < 1:
        raise ValidationFailed(f"page_count must be >= 1, got {page_count}", field="page_count")
    separators = sorted({b.page_number for b in barcodes if b.is_separator})
    for page_number in separators:
        if page_number < 1 or page_number > page_count:
            raise ValidationFailed(
                f"separator page {page_number} is outside 1..{page_count}",
                field="barcodes",
            )
    if len(separators) != sum(1 for b in barcodes if b.is_separator):
        raise ValidationFailed("duplicate separator barcodes on one page", field="barcodes")
    # Why the separator starts the next document: the sticker is the
    # cover sheet of what follows (operator prints SEP: and feeds the
    # packet behind it). A leading separator on page 1 is the normal
    # case, not an empty first document.
    boundaries = [1, *separators, page_count + 1]
    # Deduplicate a separator on page 1 (boundaries would start [1, 1]).
    deduped: list[int] = []
    for boundary in boundaries:
        if not deduped or deduped[-1] != boundary:
            deduped.append(boundary)
    return tuple((deduped[i], deduped[i + 1] - 1) for i in range(len(deduped) - 1))


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """Full-page OCR text for one page (adapter output, domain owned)."""

    page_number: int
    text: str
    confidence: float
    engine: str

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValidationFailed(
                f"ocr page_number must be >= 1, got {self.page_number}",
                field="page_number",
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed(
                f"ocr confidence must be 0..1, got {self.confidence}",
                field="confidence",
            )
        if not self.engine:
            raise ValidationFailed("ocr result requires an engine name", field="engine")

    @property
    def needs_review(self) -> bool:
        """Return True iff confidence is below the trusted threshold."""
        return self.confidence < MIN_TRUSTED_CONFIDENCE


@dataclass(frozen=True, slots=True)
class ZoneResult:
    """Zonal (or point-and-shoot) OCR text for one named zone.

    Point-and-shoot is the degenerate case: the operator clicks one
    region and the adapter reads just that zone. Same shape, one zone —
    no second code path (Constitution XV: delete the clever twin).
    """

    zone_key: str
    text: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.zone_key:
            raise ValidationFailed("zone result requires a zone_key", field="zone_key")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed(
                f"zone confidence must be 0..1, got {self.confidence}",
                field="confidence",
            )


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    """A versioned template-zone map for one form (form ID + data zones).

    ``form_id_zone`` names the zone whose text identifies the form
    (e.g. ``"FORM-1042"``); ``expected_form_id`` is the value that must
    match. ``data_zones`` names the extraction zones (each maps to a
    case field of the same key unless ``field_map`` renames it).
    Immutable once published — changes mint a new version (FR-L03).
    """

    key: str
    version_number: int
    form_id_zone: str
    expected_form_id: str
    data_zones: tuple[str, ...]
    field_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise ValidationFailed("template requires a key", field="key")
        if self.version_number < 1:
            raise ValidationFailed(
                f"template version_number must be >= 1, got {self.version_number}",
                field="version_number",
            )
        if not self.form_id_zone:
            raise ValidationFailed("template requires a form_id_zone", field="form_id_zone")
        if not self.expected_form_id:
            raise ValidationFailed(
                "template requires an expected_form_id", field="expected_form_id"
            )
        if not self.data_zones:
            raise ValidationFailed("template requires at least one data zone", field="data_zones")
        if len(self.data_zones) > MAX_TEMPLATE_ZONES:
            raise ValidationFailed(
                f"template supports at most {MAX_TEMPLATE_ZONES} zones",
                field="data_zones",
            )
        if len(set(self.data_zones)) != len(self.data_zones):
            raise ValidationFailed("template data_zones must be unique", field="data_zones")


@dataclass(frozen=True, slots=True)
class TemplateExtraction:
    """The field assignments one template application produced."""

    template_key: str
    template_version: int
    form_id_matched: bool
    field_values: dict[str, str]
    low_confidence_zones: tuple[str, ...]
    needs_review: bool


def apply_template(
    *,
    template: TemplateDefinition,
    zones: tuple[ZoneResult, ...],
) -> TemplateExtraction:
    """Apply a template to zonal OCR output (pure).

    The form-ID zone must match ``expected_form_id`` exactly; a
    mismatch produces an *empty* extraction with ``needs_review=True``
    (wrong form in the feeder — quarantine, never misfile). Data zones
    below ``MIN_TRUSTED_CONFIDENCE`` are still reported but listed in
    ``low_confidence_zones`` so the verifier knows which fields to
    re-key.
    """
    by_key = {zone.zone_key: zone for zone in zones}
    form_zone = by_key.get(template.form_id_zone)
    matched = form_zone is not None and form_zone.text.strip() == template.expected_form_id
    if not matched:
        return TemplateExtraction(
            template_key=template.key,
            template_version=template.version_number,
            form_id_matched=False,
            field_values={},
            low_confidence_zones=(),
            needs_review=True,
        )
    values: dict[str, str] = {}
    low: list[str] = []
    for zone_key in template.data_zones:
        zone = by_key.get(zone_key)
        if zone is None:
            low.append(zone_key)
            continue
        values[template.field_map.get(zone_key, zone_key)] = zone.text
        if zone.confidence < MIN_TRUSTED_CONFIDENCE:
            low.append(zone_key)
    return TemplateExtraction(
        template_key=template.key,
        template_version=template.version_number,
        form_id_matched=True,
        field_values=values,
        low_confidence_zones=tuple(low),
        needs_review=bool(low),
    )


@dataclass(frozen=True, slots=True)
class AutoFillSet:
    """A pre-population set: one primary value implies secondaries.

    Example: primary field ``"customer_id"`` with value ``"C-1042"``
    implies ``{"customer_name": "...", "billing_tier": "..."}``. Sets
    are versioned configuration (FR-L03); the domain only applies the
    rows the caller supplies.
    """

    name: str
    primary_field: str
    rows: dict[str, dict[str, str]]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValidationFailed("autofill set requires a name", field="name")
        if not self.primary_field:
            raise ValidationFailed("autofill set requires a primary_field", field="primary_field")
        if len(self.rows) > MAX_AUTOFILL_SECONDARIES * 20:
            raise ValidationFailed("autofill set has too many rows", field="rows")


def apply_autofill(
    *,
    autofill_set: AutoFillSet,
    primary_value: str,
) -> dict[str, str]:
    """Return the secondary assignments for ``primary_value`` (pure).

    Unknown primaries produce an empty mapping (the operator keys the
    fields by hand) — never an error, never a guess at a *similar*
    primary (a near-miss customer id is a misfile, not a help).
    """
    row = autofill_set.rows.get(primary_value)
    if row is None:
        return {}
    return dict(row)


@dataclass(frozen=True, slots=True)
class ExtractionSuggestion:
    """One bounded-assistance indexing suggestion (never authoritative).

    Carries what the engine proposed (``field_values``), how sure it
    was (``confidence``), and where the proposal came from
    (``source``: ``"barcode"`` | ``"ocr"`` | ``"template"`` |
    ``"icr"`` | ``"autofill"`` plus ``source_context`` such as the
    page number or template key). ``needs_human_verification`` is
    always True at construction — the type system, not a comment,
    enforces that no suggestion files itself (Constitution XIV).
    """

    field_values: dict[str, str]
    confidence: float
    source: str
    source_context: dict[str, str]
    actor_id: UUID | None = None
    needs_human_verification: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed(
                f"suggestion confidence must be 0..1, got {self.confidence}",
                field="confidence",
            )
        if not self.source:
            raise ValidationFailed("suggestion requires a source", field="source")
        if not self.needs_human_verification:
            raise ValidationFailed(
                "extraction suggestions always need human verification",
                field="needs_human_verification",
            )

    def describe(self) -> dict[str, object]:
        """Return a safe summary for audit payloads and logs.

        Field *keys* and confidence travel; field *values* do not —
        values may carry PII the timeline reader is not granted to see
        (Constitution V: sensitive values referenced by id, not content).
        """
        return {
            "source": self.source,
            "source_context": dict(self.source_context),
            "field_keys": sorted(self.field_values),
            "confidence": self.confidence,
            "needs_human_verification": True,
        }


__all__ = [
    "MIN_TRUSTED_CONFIDENCE",
    "MAX_AUTOFILL_SECONDARIES",
    "MAX_TEMPLATE_ZONES",
    "AutoFillSet",
    "Barcode",
    "BarcodeAssignment",
    "ExtractionSuggestion",
    "OcrPageResult",
    "TemplateDefinition",
    "TemplateExtraction",
    "ZoneResult",
    "apply_autofill",
    "apply_template",
    "assign_from_barcode",
    "split_on_separator_pages",
]
