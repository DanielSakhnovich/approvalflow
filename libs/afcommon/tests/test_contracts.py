from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    InvoiceSubmittedPayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.events import new_event_meta


def _meta():
    return new_event_meta(invoice_id="inv_1", correlation_id="corr_1")


def test_invoice_submitted_roundtrip():
    p = InvoiceSubmittedPayload(meta=_meta(), invoice={"vendor": "Bistro 19", "total": 42.0})
    assert InvoiceSubmittedPayload.model_validate(p.model_dump()) == p


def test_decision_made_carries_route_and_evidence():
    p = DecisionMadePayload(
        meta=_meta(), route="auto_approve", recommendation="approve",
        confidence=0.93, violations=[], reasoning="In policy.",
        usd_cents=4200, ceiling_cents=25000,
    )
    d = p.model_dump()
    assert d["route"] == "auto_approve" and d["usd_cents"] == 4200
    # Defaults for fixture-driven fields not set here (M4/Phase 05 additions).
    assert d["scenario"] == "" and d["department"] == ""
    assert DecisionMadePayload.model_validate(d) == p


def test_decision_made_roundtrips_scenario_and_department():
    p = DecisionMadePayload(
        meta=_meta(), route="human_review", recommendation="escalate",
        confidence=0.5, violations=["HW-02"], reasoning="Capital hardware.",
        usd_cents=950000, ceiling_cents=25000,
        scenario="payment-failure:journey-D", department="engineering-2026Q2",
    )
    d = p.model_dump()
    assert d["scenario"] == "payment-failure:journey-D"
    assert d["department"] == "engineering-2026Q2"
    assert DecisionMadePayload.model_validate(d) == p


def test_decision_made_rejects_unknown_route():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DecisionMadePayload(
            meta=_meta(), route="yolo", recommendation="approve",
            confidence=1.0, violations=[], reasoning="", usd_cents=1, ceiling_cents=1,
        )


def test_approval_resolved_verdicts():
    p = ApprovalResolvedPayload(meta=_meta(), verdict="needs_info",
                                approver_id="lena@northwind.example", comment="Client name?")
    d = p.model_dump()
    # Defaults for fixture-driven fields not set here (M4/Phase 05 additions).
    assert d["scenario"] == "" and d["department"] == ""
    assert ApprovalResolvedPayload.model_validate(d) == p


def test_approval_resolved_roundtrips_scenario_and_department():
    p = ApprovalResolvedPayload(
        meta=_meta(), verdict="approved", approver_id="lena@northwind.example",
        scenario="payment-failure:journey-D", department="engineering-2026Q2",
    )
    d = p.model_dump()
    assert d["scenario"] == "payment-failure:journey-D"
    assert d["department"] == "engineering-2026Q2"
    assert ApprovalResolvedPayload.model_validate(d) == p


def test_payment_payloads_roundtrip():
    ok = PaymentCompletedPayload(meta=_meta(), amount_cents=4200,
                                 budget_remaining_cents=4995800, department="engineering-2026Q2")
    bad = PaymentFailedPayload(meta=_meta(), reason="insufficient_budget", compensated=False)
    assert PaymentCompletedPayload.model_validate(ok.model_dump()) == ok
    assert PaymentFailedPayload.model_validate(bad.model_dump()) == bad
