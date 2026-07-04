import pytest
from pydantic import ValidationError

from services.intake.src.models import (
    InvoiceRecord,
    InvoiceStatus,
    InvoiceSubmission,
    to_cents,
)

FIXTURE = {
    "id": "INV-1001",
    "submitter": "dana.cohen@northwind.example",
    "department": "engineering-2026Q2",
    "vendor": "Bistro 19",
    "vendorKnown": True,
    "invoiceNumber": "NW-INV-7781",
    "currency": "USD",
    "category": "meals",
    "attendees": 1,
    "lineItems": [{"description": "Team lunch", "quantity": 1, "unitPrice": 38.89}],
    "taxAmount": 3.11,
    "total": 42.0,
    "receiptPresent": True,
    "date": "2026-05-12",
    "notes": "Solo working lunch.",
}


def test_to_cents_exact():
    assert to_cents(42.0) == 4200
    assert to_cents(38.89) == 3889
    assert to_cents("0.1") == 10
    assert to_cents(0) == 0


def test_to_cents_rejects_sub_cent_amounts():
    with pytest.raises(ValueError):
        to_cents(1.005)


def test_submission_parses_fixture_shape():
    sub = InvoiceSubmission.model_validate(FIXTURE)
    assert sub.vendor == "Bistro 19"
    assert sub.total_cents == 4200
    assert sub.line_items[0].unit_price_cents == 3889
    assert sub.client_ref == "INV-1001"


def test_submission_preserves_unknown_fields():
    sub = InvoiceSubmission.model_validate({**FIXTURE, "scenario": "auto-approve:x"})
    assert sub.model_dump(by_alias=True)["scenario"] == "auto-approve:x"


def test_submission_requires_core_fields():
    with pytest.raises(ValidationError):
        InvoiceSubmission.model_validate({"vendor": "X"})


def test_record_defaults():
    sub = InvoiceSubmission.model_validate(FIXTURE)
    rec = InvoiceRecord.new(invoice_id="inv_abc", correlation_id="corr_1", submission=sub)
    assert rec.status == InvoiceStatus.SUBMITTED
    assert rec.client_ref == "INV-1001"
    assert rec.route is None and rec.reasoning == ""
    assert InvoiceRecord.model_validate(rec.model_dump()) == rec
