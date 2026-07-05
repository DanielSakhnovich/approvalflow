import json
from pathlib import Path

from services.decision.src.config import Thresholds
from services.decision.src.validators import validate


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
