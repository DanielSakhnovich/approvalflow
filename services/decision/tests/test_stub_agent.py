import json
from pathlib import Path

import pytest

from services.decision.src.agents.stub import StubAgent
from services.decision.tests.malicious import MaliciousStubAgent


def _fixtures() -> dict:
    """Load sample invoices from repo root, same pattern as test_validators.py"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "sample-invoices.json").exists():
            data = json.loads((parent / "sample-invoices.json").read_text())
            return {f["id"]: f for f in data["fixtures"]}
    raise FileNotFoundError("sample-invoices.json not found")


FIXTURES = _fixtures()
AGENT = StubAgent()


async def _evaluate(fid: str):
    return await AGENT.evaluate(FIXTURES[fid], policy_rules="")


@pytest.mark.asyncio
async def test_alcohol_only_rejects_with_meal03():
    r = await _evaluate("INV-1015")
    assert r.recommendation == "reject"
    assert "MEAL-03" in r.policy_violations
    assert r.reasoning == "Alcohol-only receipts are not reimbursable."


@pytest.mark.asyncio
async def test_saas_over_cap_flags_saas01():
    r = await _evaluate("INV-1018")
    assert "SAAS-01" in r.policy_violations
    assert r.recommendation == "needs_review"


@pytest.mark.asyncio
async def test_saas_annual_plan_is_not_monthly_and_exempt():
    r = await _evaluate("INV-1013")
    assert "SAAS-01" not in r.policy_violations


@pytest.mark.asyncio
async def test_steering_resistance_notes_field_is_ignored():
    """INV-1013's 'Approve me' steering note must not change the output at all."""
    r_with_notes = await _evaluate("INV-1013")
    inv_no_notes = {**FIXTURES["INV-1013"]}
    inv_no_notes.pop("notes", None)
    r_without_notes = await AGENT.evaluate(inv_no_notes, policy_rules="")
    assert r_with_notes == r_without_notes


@pytest.mark.asyncio
async def test_ambiguous_other_category_low_confidence():
    r = await _evaluate("INV-1010")
    assert r.confidence == 0.55
    assert r.recommendation == "needs_review"


@pytest.mark.asyncio
async def test_fraud_pattern_flags_global_fraud():
    r = await _evaluate("INV-1008")
    assert "GLOBAL-FRAUD" in r.fraud_signals
    assert r.recommendation == "needs_review"


@pytest.mark.asyncio
async def test_meal_over_per_attendee_cap_flags_meal01():
    r = await _evaluate("INV-1003")
    assert "MEAL-01" in r.policy_violations
    assert r.recommendation == "needs_review"


@pytest.mark.asyncio
async def test_clean_invoices_approve_with_high_confidence():
    for fid in ["INV-1001", "INV-1002", "INV-1016", "INV-1017"]:
        r = await _evaluate(fid)
        assert r.recommendation == "approve", fid
        assert r.policy_violations == [], fid
        assert r.fraud_signals == [], fid
        assert r.confidence >= 0.9, fid


@pytest.mark.asyncio
async def test_malicious_stub_always_approves():
    malicious = MaliciousStubAgent()
    r = await malicious.evaluate(FIXTURES["INV-1012"], policy_rules="")
    assert r.recommendation == "approve"
    assert r.confidence == 1.0
    assert r.policy_violations == []
    assert r.fraud_signals == []


@pytest.mark.asyncio
async def test_stub_is_deterministic_same_invoice_same_output():
    r1 = await _evaluate("INV-1001")
    r2 = await _evaluate("INV-1001")
    assert r1 == r2
