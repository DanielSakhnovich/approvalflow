import json
from pathlib import Path

from services.decision.src.config import Thresholds
from services.decision.src.validators import ValidationResult, validate


def _fixtures() -> tuple:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "sample-invoices.json").exists():
            data = json.loads((parent / "sample-invoices.json").read_text())
            return {f["id"]: f for f in data["fixtures"]}, data["fxRates"]
    raise FileNotFoundError


FIXTURES, FX = _fixtures()
T = Thresholds()


def v(fid: str):
    return validate(FIXTURES[fid], FX, T)


def test_clean_invoice_no_stops():
    r = v("INV-1001")
    assert r.hard_stops == [] and r.usd_cents == 4200


def test_missing_receipt():
    assert v("INV-1005").hard_stops == ["GLOBAL-RECEIPT"]


def test_math_mismatch():
    assert "GLOBAL-MATH" in v("INV-1006").hard_stops


def test_fx_conversion_and_stop():
    r = v("INV-1009")
    assert r.usd_cents == 129600 and "GLOBAL-FX" in r.hard_stops


def test_unknown_vendor():
    assert v("INV-1011").hard_stops == ["GLOBAL-VENDOR"]


def test_travel_over_1500():
    assert "TRAVEL-02" in v("INV-1019").hard_stops


def test_meals_missing_attendees():
    inv = {**FIXTURES["INV-1001"]}
    inv.pop("attendees")
    assert "MEAL-01" in validate(inv, FX, T).hard_stops


def test_unknown_currency_is_fx_stop():
    inv = {**FIXTURES["INV-1001"], "currency": "CHF"}
    assert "GLOBAL-FX" in validate(inv, FX, T).hard_stops


def test_receipt_boundary():
    base = {**FIXTURES["INV-1001"], "receiptPresent": False, "taxAmount": 0.0,
            "lineItems": [{"description": "x", "quantity": 1, "unitPrice": 25.00}],
            "total": 25.00}
    assert validate(base, FX, T).hard_stops == []
    over = {**base, "total": 25.01,
            "lineItems": [{"description": "x", "quantity": 1, "unitPrice": 25.01}]}
    assert "GLOBAL-RECEIPT" in validate(over, FX, T).hard_stops


# --- Malformed-content regression tests -------------------------------------
#
# validate() must never raise on invoice content, and malformed content must
# never be silently coerced into a passing/auto-approvable result.


def test_null_total_is_malformed_hard_stop():
    inv = {**FIXTURES["INV-1001"], "total": None}
    r = validate(inv, FX, T)
    assert "GLOBAL-MALFORMED" in r.hard_stops


def test_null_line_items_is_malformed_hard_stop():
    inv = {**FIXTURES["INV-1001"], "lineItems": None}
    r = validate(inv, FX, T)
    assert "GLOBAL-MALFORMED" in r.hard_stops


def test_non_numeric_unit_price_is_malformed_hard_stop():
    inv = {**FIXTURES["INV-1001"],
           "lineItems": [{"description": "x", "quantity": 1, "unitPrice": "abc"}]}
    r = validate(inv, FX, T)
    assert "GLOBAL-MALFORMED" in r.hard_stops


def test_null_tax_amount_is_malformed_hard_stop():
    inv = {**FIXTURES["INV-1001"], "taxAmount": None}
    r = validate(inv, FX, T)
    assert "GLOBAL-MALFORMED" in r.hard_stops


def test_hostile_battery_never_raises_and_always_flags_malformed():
    # Fully empty invoice: `total` is entirely absent, which is just as
    # unusable as an explicit null -- validate() must still return (not
    # raise) and must still escalate rather than silently treat it as $0.
    r_empty = validate({}, FX, T)
    assert isinstance(r_empty, ValidationResult)
    assert "GLOBAL-MALFORMED" in r_empty.hard_stops

    # Wrong-typed everything: dict where a number is expected, a list
    # containing an empty dict for lineItems, an int for currency.
    r_hostile = validate({"total": {}, "lineItems": [{}], "currency": 7}, FX, T)
    assert isinstance(r_hostile, ValidationResult)
    assert "GLOBAL-MALFORMED" in r_hostile.hard_stops

    # Non-finite and oversized numerics: these construct as Decimals just
    # fine (so naive parse-time checks miss them) but crash quantize/int
    # downstream -- each must be flagged malformed, never raise.
    for bad in [float("inf"), float("nan"), 1e50, "1e50", 10**40]:
        r_total = validate({**FIXTURES["INV-1001"], "total": bad}, FX, T)
        assert isinstance(r_total, ValidationResult)
        assert "GLOBAL-MALFORMED" in r_total.hard_stops, repr(bad)

        r_price = validate(
            {**FIXTURES["INV-1001"],
             "lineItems": [{"description": "x", "quantity": 1, "unitPrice": bad}]},
            FX, T,
        )
        assert isinstance(r_price, ValidationResult)
        assert "GLOBAL-MALFORMED" in r_price.hard_stops, repr(bad)


def test_null_total_can_never_auto_approve():
    # CRITICAL SAFETY ASSERTION: a null/unparseable `total` must never be
    # silently coerced to 0 and passed through as a clean, auto-approvable
    # result. If we had coerced malformed numerics to a bare 0 without also
    # forcing a hard stop, a garbage/missing amount would look exactly like
    # a legitimate $0.00 invoice -- i.e. it would slip *under* every
    # autonomy ceiling and could be auto-approved without a human ever
    # looking at it. Asserting hard_stops is non-empty here is what
    # guarantees that can't happen: any gate that only auto-approves when
    # hard_stops == [] will correctly refuse this invoice.
    inv = {**FIXTURES["INV-1001"], "total": None}
    r = validate(inv, FX, T)
    assert r.hard_stops != []
    assert "GLOBAL-MALFORMED" in r.hard_stops
