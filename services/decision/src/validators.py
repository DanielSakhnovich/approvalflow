from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from services.decision.src.config import Thresholds


class ValidationResult(BaseModel):
    usd_cents: int
    hard_stops: list[str]
    notes: list[str]


def _to_cents(value: float | int | Decimal) -> int:
    """Convert dollar amount to cents using ROUND_HALF_UP."""
    d = Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate(invoice: dict, fx_rates: dict, thresholds: Thresholds) -> ValidationResult:
    """
    Validate invoice deterministically. Pure function: never raises on content.
    Returns ValidationResult with usd_cents, hard_stops (in stable order), and notes.

    Rule order for hard stops:
    1. GLOBAL-FX: currency conversion errors, missing rates, or exceeding FX limits
    2. GLOBAL-MATH: line item + tax sum != total
    3. GLOBAL-RECEIPT: amount > $25 without receipt
    4. GLOBAL-VENDOR: vendor not known
    5. MEAL-01: meals category without attendees
    6. TRAVEL-02: travel category > $1500
    """
    hard_stops = []
    notes = []

    # Step 1: FX conversion to USD cents
    currency = invoice.get("currency", "USD")
    total = invoice.get("total", 0.0)

    if currency == "USD":
        usd_cents = _to_cents(total)
    else:
        # Non-USD currency: must look up FX rate
        if currency not in fx_rates:
            hard_stops.append("GLOBAL-FX")
            notes.append(f"Missing FX rate for currency: {currency}")
            # Fallback: use total * 100 for other checks
            usd_cents = _to_cents(total)
        else:
            rate = fx_rates[currency]
            converted_dollars = Decimal(str(total)) * Decimal(str(rate))
            usd_cents = _to_cents(converted_dollars)

            # Check if converted amount exceeds hard limits
            if usd_cents > 100000 or usd_cents > thresholds.ceiling_cents:
                hard_stops.append("GLOBAL-FX")
                notes.append(
                    f"Converted amount exceeds limits: {usd_cents} cents "
                    f"(> max({100000}, {thresholds.ceiling_cents}))"
                )

    # Step 2: GLOBAL-MATH check
    line_items = invoice.get("lineItems", [])
    tax_amount = invoice.get("taxAmount", 0.0)

    calculated_cents = 0
    for item in line_items:
        quantity = item.get("quantity", 0)
        unit_price = item.get("unitPrice", 0.0)
        item_total = Decimal(str(quantity)) * Decimal(str(unit_price))
        calculated_cents += _to_cents(item_total)

    tax_cents = _to_cents(tax_amount)
    total_cents = _to_cents(total)

    if calculated_cents + tax_cents != total_cents:
        hard_stops.append("GLOBAL-MATH")
        notes.append(
            f"Math mismatch: sum of line items ({calculated_cents}) + "
            f"tax ({tax_cents}) = {calculated_cents + tax_cents}, "
            f"expected total ({total_cents})"
        )

    # Step 3: GLOBAL-RECEIPT check
    receipt_present = invoice.get("receiptPresent", False)
    if usd_cents > 2500 and not receipt_present:
        hard_stops.append("GLOBAL-RECEIPT")
        notes.append(f"Receipt required for amounts > $25.00 ({usd_cents} cents)")

    # Step 4: GLOBAL-VENDOR check
    vendor_known = invoice.get("vendorKnown", False)
    if not vendor_known:
        hard_stops.append("GLOBAL-VENDOR")
        notes.append("Vendor is unknown")

    # Step 5: MEAL-01 check (missing attendees for meals)
    category = invoice.get("category", "")
    if category == "meals":
        attendees = invoice.get("attendees")
        if attendees is None:
            hard_stops.append("MEAL-01")
            notes.append("Meals category requires attendees field")

    # Step 6: TRAVEL-02 check (travel > $1500)
    if category == "travel" and usd_cents > 150000:
        hard_stops.append("TRAVEL-02")
        notes.append("Travel expenses over $1,500 require manager approval")

    return ValidationResult(
        usd_cents=usd_cents,
        hard_stops=hard_stops,
        notes=notes,
    )
