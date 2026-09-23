"""Pure-domain tests for assisted indexing (T1 / 002-leftovers).

Covers the rules in ``app.domain.extraction`` plus the adapter seam in
``app.domain.ports.extraction``:

- barcode-driven field assignment (mapped value, unknown value ->
  empty + review, low confidence -> review);
- separator-page splitting partitions the batch (cover-sheet semantics,
  duplicate/out-of-range separators refuse);
- OCR confidence gating (``needs_review`` below threshold);
- template-zone extraction (form-ID match/mismatch, low-confidence
  zones listed, field renames applied);
- AutoFill primary -> secondaries (unknown primary -> empty, never a guess);
- suggestions always need human verification and describe audit-safe
  summaries (keys travel, values do not);
- fakes implementing the Protocols prove the seam works with no vendor
  SDK in the domain.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Structure is ``// given`` / ``// when`` / ``// then``. Deterministic: no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import pytest

from app.domain.errors import ValidationFailed
from app.domain.extraction import (
    AutoFillSet,
    Barcode,
    ExtractionSuggestion,
    OcrPageResult,
    TemplateDefinition,
    ZoneResult,
    apply_autofill,
    apply_template,
    assign_from_barcode,
    split_on_separator_pages,
)
from app.domain.ports.extraction import (
    BarcodeDecoder,
    HandwritingRecognizer,
    OcrProvider,
    TemplateZoneExtractor,
)


def _barcode(value: str, page: int = 1, confidence: float = 1.0) -> Barcode:
    """Build a code128 barcode for assignment tests."""
    return Barcode(symbology="code128", value=value, page_number=page, confidence=confidence)


def _template() -> TemplateDefinition:
    """Build a single-version form template for zone tests."""
    return TemplateDefinition(
        key="intake-form",
        version_number=2,
        form_id_zone="form_id",
        expected_form_id="FORM-1042",
        data_zones=("name", "priority"),
        field_map={"name": "title"},
    )


# ---------------------------------------------------------------------------
# Barcode-driven assignment
# ---------------------------------------------------------------------------


def test_assign_from_barcode__mapped_value__returns_fields() -> None:
    """A mapped barcode pre-populates the configured fields."""
    # // given a barcode + mapping row // when assigning // then fields land.
    assignment = assign_from_barcode(
        barcode=_barcode("CUST-7"),
        mapping={"CUST-7": {"customer": "CUST-7", "priority": "high"}},
        case_type_key="ServiceRequest",
    )
    assert assignment.case_type_key == "ServiceRequest"
    assert assignment.field_values == {"customer": "CUST-7", "priority": "high"}
    assert assignment.needs_review is False


def test_assign_from_barcode__unknown_value__returns_empty_needing_review() -> None:
    """An unmapped barcode indexes by hand — never dropped, never guessed."""
    # // given an unmapped barcode // when assigning // then empty + review.
    assignment = assign_from_barcode(
        barcode=_barcode("STICKER-???"),
        mapping={"CUST-7": {"customer": "CUST-7"}},
        case_type_key="ServiceRequest",
    )
    assert assignment.field_values == {}
    assert assignment.needs_review is True


def test_assign_from_barcode__shaky_read__flags_review() -> None:
    """A low-confidence decode flags review even when the value maps."""
    # // given a low-confidence barcode // when assigning // then review.
    assignment = assign_from_barcode(
        barcode=_barcode("CUST-7", confidence=0.42),
        mapping={"CUST-7": {"customer": "CUST-7"}},
        case_type_key="ServiceRequest",
    )
    assert assignment.field_values == {"customer": "CUST-7"}
    assert assignment.needs_review is True


# ---------------------------------------------------------------------------
# Separator-page splitting
# ---------------------------------------------------------------------------


def test_split_on_separator_pages__two_packets__partitions_batch() -> None:
    """Separators start new documents; ranges fully partition the batch."""
    # // given a 6-page batch with a separator on page 4 // when splitting
    # // then two ranges partition 1..6.
    ranges = split_on_separator_pages(
        page_count=6,
        barcodes=(_barcode("SEP:packet-b", page=4),),
    )
    assert ranges == ((1, 3), (4, 6))


def test_split_on_separator_pages__no_separator__single_range() -> None:
    """A batch without separators is one document."""
    # // given no separators // when splitting // then the whole batch.
    assert split_on_separator_pages(page_count=3, barcodes=()) == ((1, 3),)


def test_split_on_separator_pages__out_of_range__raises() -> None:
    """A separator beyond the batch is a misfeed signal, not a boundary."""
    # // given a separator past the last page // when splitting // then refused.
    with pytest.raises(ValidationFailed, match="outside"):
        split_on_separator_pages(page_count=2, barcodes=(_barcode("SEP:x", page=9),))


# ---------------------------------------------------------------------------
# OCR confidence gating
# ---------------------------------------------------------------------------


def test_ocr_page_result__low_confidence__needs_review() -> None:
    """OCR below the trusted threshold must be reviewed before indexing."""
    # // given a shaky full-page read // when checking // then review flagged.
    result = OcrPageResult(page_number=2, text="hello", confidence=0.51, engine="fake-ocr")
    assert result.needs_review is True
    confident = OcrPageResult(page_number=2, text="hello", confidence=0.99, engine="fake-ocr")
    assert confident.needs_review is False


# ---------------------------------------------------------------------------
# Template zones
# ---------------------------------------------------------------------------


def test_apply_template__matching_form__extracts_renamed_fields() -> None:
    """A matching form ID extracts data zones with field renames applied."""
    # // given a matching form-id zone // when applying // then fields land.
    extraction = apply_template(
        template=_template(),
        zones=(
            ZoneResult(zone_key="form_id", text="FORM-1042", confidence=0.99),
            ZoneResult(zone_key="name", text="Acme example", confidence=0.95),
            ZoneResult(zone_key="priority", text="high", confidence=0.40),
        ),
    )
    assert extraction.form_id_matched is True
    assert extraction.field_values == {"title": "Acme example", "priority": "high"}
    assert extraction.low_confidence_zones == ("priority",)
    assert extraction.needs_review is True


def test_apply_template__wrong_form__quarantines_with_empty_fields() -> None:
    """A form-ID mismatch quarantines — never misfiles into the wrong form."""
    # // given a foreign form-id zone // when applying // then empty + review.
    extraction = apply_template(
        template=_template(),
        zones=(ZoneResult(zone_key="form_id", text="FORM-9999", confidence=0.99),),
    )
    assert extraction.form_id_matched is False
    assert extraction.field_values == {}
    assert extraction.needs_review is True


def test_template_definition__duplicate_zones__raises() -> None:
    """Duplicate zone keys would make extraction ambiguous — refused."""
    # // given duplicated data zones // when constructing // then refused.
    with pytest.raises(ValidationFailed, match="unique"):
        TemplateDefinition(
            key="dup",
            version_number=1,
            form_id_zone="form_id",
            expected_form_id="F",
            data_zones=("name", "name"),
        )


# ---------------------------------------------------------------------------
# AutoFill sets
# ---------------------------------------------------------------------------


def test_apply_autofill__known_primary__returns_secondaries() -> None:
    """A known primary pre-populates its secondaries."""
    # // given an autofill set // when applying a known primary // then mapped.
    autofill = AutoFillSet(
        name="customers",
        primary_field="customer_id",
        rows={"C-1042": {"customer_name": "Acme", "billing_tier": "gold"}},
    )
    assert apply_autofill(autofill_set=autofill, primary_value="C-1042") == {
        "customer_name": "Acme",
        "billing_tier": "gold",
    }


def test_apply_autofill__unknown_primary__returns_empty_never_guess() -> None:
    """An unknown primary returns empty — a near-miss id is a misfile."""
    # // given an unknown primary // when applying // then empty mapping.
    autofill = AutoFillSet(
        name="customers",
        primary_field="customer_id",
        rows={"C-1042": {"customer_name": "Acme"}},
    )
    assert apply_autofill(autofill_set=autofill, primary_value="C-1043") == {}


# ---------------------------------------------------------------------------
# Bounded-assistance suggestions
# ---------------------------------------------------------------------------


def test_suggestion__always_needs_verification__cannot_opt_out() -> None:
    """The type system enforces human authority — no self-filing suggestion."""
    # // given suggestion fields // when opting out of verification // then refused.
    with pytest.raises(ValidationFailed, match="human verification"):
        ExtractionSuggestion(
            field_values={"title": "x"},
            confidence=0.99,
            source="ocr",
            source_context={"page": "1"},
            needs_human_verification=False,
        )


def test_suggestion_describe__omits_values__audit_safe() -> None:
    """Audit summaries carry keys + confidence, never field values."""
    # // given a suggestion with PII-ish values // when describing // then redacted.
    suggestion = ExtractionSuggestion(
        field_values={"customer_name": "Acme example"},
        confidence=0.91,
        source="template",
        source_context={"template": "intake-form"},
    )
    summary = suggestion.describe()
    assert summary["field_keys"] == ["customer_name"]
    assert "Acme example" not in str(summary.values())


# ---------------------------------------------------------------------------
# Adapter seam fakes (no vendor SDK in domain)
# ---------------------------------------------------------------------------


class _FakeBarcodeDecoder(BarcodeDecoder):
    """In-memory barcode fake: canned values keyed by storage ref."""

    def __init__(self, canned: dict[str, list[Barcode]]) -> None:
        self._canned = canned

    async def decode_page(
        self, tenant_id: UUID, storage_ref: str, page_number: int
    ) -> Sequence[Barcode]:
        return tuple(self._canned.get(storage_ref, []))


class _FakeOcrProvider(OcrProvider):
    """In-memory OCR fake: canned full-page + zonal readings."""

    def __init__(
        self,
        pages: dict[str, OcrPageResult],
        zones: dict[str, list[ZoneResult]],
    ) -> None:
        self._pages = pages
        self._zones = zones

    async def read_full_page(
        self, tenant_id: UUID, storage_ref: str, page_number: int
    ) -> OcrPageResult:
        return self._pages[storage_ref]

    async def read_zones(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        zone_keys: Sequence[str],
    ) -> Sequence[ZoneResult]:
        wanted = set(zone_keys)
        return tuple(z for z in self._zones.get(storage_ref, []) if z.zone_key in wanted)


class _FakeTemplateExtractor(TemplateZoneExtractor):
    """Template fake: delegates to the canned OCR-zone fake."""

    def __init__(self, zones: dict[str, list[ZoneResult]]) -> None:
        self._zones = zones

    async def extract(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        template: TemplateDefinition,
    ) -> Sequence[ZoneResult]:
        wanted = {template.form_id_zone, *template.data_zones}
        return tuple(z for z in self._zones.get(storage_ref, []) if z.zone_key in wanted)


class _FakeIcr(HandwritingRecognizer):
    """ICR fake: canned handwriting readings; empty when engine unwired."""

    def __init__(self, zones: dict[str, list[ZoneResult]]) -> None:
        self._zones = zones

    async def read_handwriting(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        zone_keys: Sequence[str],
    ) -> Sequence[ZoneResult]:
        wanted = set(zone_keys)
        return tuple(z for z in self._zones.get(storage_ref, []) if z.zone_key in wanted)


async def test_seam_fakes__barcode_to_split__drives_workspace() -> None:
    """Fake decoder output feeds separator splitting with no vendor code."""
    # // given a fake decoder with a separator // when decoding + splitting
    # // then the workspace ranges partition the batch.
    tenant = UUID("11111111-1111-1111-1111-111111111111")
    decoder = _FakeBarcodeDecoder({"ref-batch": [_barcode("SEP:packet-b", page=3)]})
    found = await decoder.decode_page(tenant, "ref-batch", 3)
    ranges = split_on_separator_pages(page_count=5, barcodes=tuple(found))
    assert ranges == ((1, 2), (3, 5))


async def test_seam_fakes__ocr_template_icr__compose() -> None:
    """Fake OCR + template + ICR outputs compose into reviewed extractions."""
    # // given canned OCR/template/ICR readings // when extracting
    # // then template matches and the handwritten zone needs review.
    tenant = UUID("11111111-1111-1111-1111-111111111111")
    zone_rows = [
        ZoneResult(zone_key="form_id", text="FORM-1042", confidence=0.99),
        ZoneResult(zone_key="name", text="Acme example", confidence=0.95),
        ZoneResult(zone_key="priority", text="high", confidence=0.97),
    ]
    ocr = _FakeOcrProvider(
        pages={"ref-p1": OcrPageResult(page_number=1, text="full", confidence=0.9, engine="fake")},
        zones={"ref-p1": zone_rows},
    )
    extractor = _FakeTemplateExtractor({"ref-p1": zone_rows})
    icr = _FakeIcr({"ref-p1": [ZoneResult(zone_key="signature", text="J. Doe", confidence=0.55)]})

    page = await ocr.read_full_page(tenant, "ref-p1", 1)
    assert page.needs_review is False
    template_zones = await extractor.extract(tenant, "ref-p1", 1, _template())
    extraction = apply_template(template=_template(), zones=tuple(template_zones))
    assert extraction.form_id_matched is True
    assert extraction.needs_review is False
    handwriting = await icr.read_handwriting(tenant, "ref-p1", 1, ["signature"])
    assert handwriting[0].confidence < 0.80
