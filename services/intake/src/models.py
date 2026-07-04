from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def to_cents(x: float | int | str) -> int:
    d = Decimal(str(x))
    quantized = d.quantize(Decimal("0.01"))
    if quantized != d:
        raise ValueError(f"amount {x!r} has sub-cent precision")
    return int(quantized * 100)


class LineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    description: str
    quantity: float
    unit_price: float = Field(alias="unitPrice")

    @property
    def unit_price_cents(self) -> int:
        return to_cents(self.unit_price)


class InvoiceSubmission(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    client_ref: str | None = Field(default=None, alias="id")
    submitter: str
    department: str
    vendor: str
    vendor_known: bool = Field(alias="vendorKnown")
    invoice_number: str = Field(alias="invoiceNumber")
    currency: str
    category: str
    line_items: list[LineItem] = Field(alias="lineItems")
    tax_amount: float = Field(alias="taxAmount")
    total: float
    receipt_present: bool = Field(alias="receiptPresent")
    date: str
    notes: str = ""

    @property
    def total_cents(self) -> int:
        return to_cents(self.total)


class InvoiceStatus(StrEnum):
    SUBMITTED = "submitted"
    EVALUATING = "evaluating"
    PENDING_APPROVAL = "pending_approval"
    NEEDS_INFO = "needs_info"
    APPROVED = "approved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    SUBMIT_FAILED = "submit_failed"


class InvoiceRecord(BaseModel):
    invoice_id: str
    client_ref: str | None
    correlation_id: str
    status: InvoiceStatus
    submission: dict[str, Any]
    route: str | None = None
    decided_by: str | None = None
    reasoning: str = ""
    created_at: str
    updated_at: str

    @classmethod
    def new(cls, invoice_id: str, correlation_id: str,
            submission: InvoiceSubmission) -> "InvoiceRecord":
        now = datetime.now(UTC).isoformat()
        return cls(
            invoice_id=invoice_id,
            client_ref=submission.client_ref,
            correlation_id=correlation_id,
            status=InvoiceStatus.SUBMITTED,
            submission=submission.model_dump(by_alias=True),
            created_at=now,
            updated_at=now,
        )
