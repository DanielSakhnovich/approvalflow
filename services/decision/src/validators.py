"""
Deterministic, pure invoice validators.

Malformed-content handling
---------------------------
`validate()` must never raise on invoice content, including missing,
null, or non-numeric fields produced by upstream systems. Silently
coercing a malformed numeric field (e.g. `total: null`) to 0 would be
dangerous on its own: a garbage/missing amount would look like a $0
invoice and could slip *under* the autonomy ceiling, triggering
auto-approval of content nobody actually validated. Instead, any
unparseable numeric or list field is recorded and forces a
`GLOBAL-MALFORMED` hard stop, so malformed content is always STRICTLY
MORE escalated (never less) than well-formed content. The internal 0
placeholder used for a malformed field is safe only *because* that
accompanying hard stop guarantees human review regardless of what any
downstream amount comparison concludes.

Field-by-field handling:
- `total`, `taxAmount`, line item `quantity` / `unitPrice`: parsed via
  `Decimal(str(value))`; parse failures (`None`, dict, list,
  non-numeric string, etc.) are flagged malformed and treated as 0 for
  the purposes of the (already-failing) math/receipt/travel checks.
  Non-finite values (Infinity, NaN) and oversized magnitudes that
  exceed the cents-conversion precision (e.g. 1e50, 10**40) are also
  flagged malformed rather than crashing quantize/int downstream.
- `lineItems`: must be a list of dicts. `None`, a non-list value, or a
  list containing non-dict items is flagged malformed and treated as
  an empty list for the line-item math check.
- `currency`: defensively coerced with `str(...)` when not already a
  string. This is not flagged as malformed: `currency` is only ever
  used for an `==` comparison and an `in`/lookup against `fx_rates`,
  so coercing it to a string keeps those operations well-defined
  (notably, avoids a `TypeError` from an unhashable value such as a
  dict) without needing to escalate — an unknown/garbled currency
  already produces a `GLOBAL-FX` hard stop via the existing "missing
  rate" path.
- `category`, `receiptPresent`, `vendorKnown`, `attendees`: only ever
  compared with `==`/`is`/truthiness, which cannot raise regardless of
  type, so these are left as-is (no coercion needed for purity).
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel

from services.decision.src.config import Thresholds

GLOBAL_MALFORMED = "GLOBAL-MALFORMED"


class ValidationResult(BaseModel):
    usd_cents: int
    hard_stops: list[str]
    notes: list[str]


def _flag_malformed(field_name: str, malformed: list[str]) -> None:
    """Record `field_name` as malformed at most once, preserving first-seen order."""
    if field_name not in malformed:
        malformed.append(field_name)


def _safe_decimal(value: object, field_name: str, malformed: list[str]) -> Decimal:
    """
    Parse `value` as a Decimal. On failure (None, dict, list, non-numeric
    string, etc.) flag `field_name` as malformed and return Decimal(0) as a
    safe placeholder -- safe only because the caller must also surface the
    GLOBAL-MALFORMED hard stop derived from `malformed`.

    Non-finite values (`Infinity`, `NaN`) construct successfully but are not
    usable dollar amounts and would crash quantize/int downstream, so they
    are rejected here as malformed too.
    """
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise InvalidOperation
        return d
    except (InvalidOperation, ValueError, TypeError):
        _flag_malformed(field_name, malformed)
        return Decimal(0)


def _to_cents(d: Decimal, field_name: str, malformed: list[str]) -> int:
    """
    Convert a dollar-amount Decimal to integer cents using ROUND_HALF_UP.

    Belt-and-braces choke point: even finite Decimals can fail conversion
    (e.g. oversized magnitudes like 1e50 exceed the quantize context
    precision and raise InvalidOperation). Any failure flags `field_name`
    as malformed and returns the 0 placeholder -- again safe only because
    the accompanying GLOBAL-MALFORMED hard stop forces human review.
    """
    try:
        return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        _flag_malformed(field_name, malformed)
        return 0


def validate(invoice: dict, fx_rates: dict, thresholds: Thresholds) -> ValidationResult:
    """
    Validate invoice deterministically. Pure function: never raises on content.
    Returns ValidationResult with usd_cents, hard_stops (in stable order), and notes.

    Rule order for hard stops:
    1. GLOBAL-MALFORMED: unparseable or missing numeric/list fields (validator-local
       hard stop; see module docstring -- always appears first when present)
    2. GLOBAL-FX: currency conversion errors, missing rates, or exceeding FX limits
    3. GLOBAL-MATH: line item + tax sum != total
    4. GLOBAL-RECEIPT: amount > $25 without receipt
    5. GLOBAL-VENDOR: vendor not known
    6. MEAL-01: meals category without attendees
    7. TRAVEL-02: travel category > $1500
    """
    hard_stops: list[str] = []
    notes: list[str] = []
    malformed: list[str] = []

    # Step 1: FX conversion to USD cents
    currency = invoice.get("currency", "USD")
    if not isinstance(currency, str):
        currency = str(currency)
    # `total` has no sane default: unlike an omitted `lineItems`/`taxAmount`
    # (where "none provided" plausibly means zero), a wholly-missing total is
    # just as unusable as an explicit `null` and must flag the same way.
    total = invoice.get("total")
    total_decimal = _safe_decimal(total, "total", malformed)

    if currency == "USD":
        usd_cents = _to_cents(total_decimal, "total", malformed)
    else:
        # Non-USD currency: must look up FX rate
        if currency not in fx_rates:
            hard_stops.append("GLOBAL-FX")
            notes.append(f"Missing FX rate for currency: {currency}")
            # Fallback: use total * 100 for other checks
            usd_cents = _to_cents(total_decimal, "total", malformed)
        else:
            rate = fx_rates[currency]
            rate_decimal = _safe_decimal(rate, "fxRate", malformed)
            converted_dollars = total_decimal * rate_decimal
            usd_cents = _to_cents(converted_dollars, "total", malformed)

            # Check if converted amount exceeds hard limits
            if usd_cents > 100000 or usd_cents > thresholds.ceiling_cents:
                hard_stops.append("GLOBAL-FX")
                notes.append(
                    f"Converted amount exceeds limits: {usd_cents} cents "
                    f"(> max({100000}, {thresholds.ceiling_cents}))"
                )

    # Step 2: GLOBAL-MATH check
    line_items = invoice.get("lineItems", [])
    if not isinstance(line_items, list):
        _flag_malformed("lineItems", malformed)
        line_items = []
    tax_amount = invoice.get("taxAmount", 0.0)

    calculated_cents = 0
    for item in line_items:
        if not isinstance(item, dict):
            _flag_malformed("lineItems", malformed)
            continue
        quantity = item.get("quantity", 0)
        unit_price = item.get("unitPrice", 0.0)
        quantity_decimal = _safe_decimal(quantity, "lineItems", malformed)
        unit_price_decimal = _safe_decimal(unit_price, "lineItems", malformed)
        item_total = quantity_decimal * unit_price_decimal
        calculated_cents += _to_cents(item_total, "lineItems", malformed)

    tax_cents = _to_cents(
        _safe_decimal(tax_amount, "taxAmount", malformed), "taxAmount", malformed
    )
    total_cents = _to_cents(total_decimal, "total", malformed)

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

    # Step 0 (surfaced first): GLOBAL-MALFORMED -- unparseable/missing content
    # is always a hard stop, prepended ahead of every other rule so malformed
    # invoices can never be less escalated than well-formed ones.
    if malformed:
        hard_stops.insert(0, GLOBAL_MALFORMED)
        malformed_notes = [
            f"Unparseable or missing numeric/list field {field} — cannot be auto-approved."
            for field in malformed
        ]
        notes = malformed_notes + notes

    return ValidationResult(
        usd_cents=usd_cents,
        hard_stops=hard_stops,
        notes=notes,
    )
