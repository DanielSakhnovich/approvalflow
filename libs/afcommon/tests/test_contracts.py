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
    assert ApprovalResolvedPayload.model_validate(p.model_dump()) == p


def test_payment_payloads_roundtrip():
    ok = PaymentCompletedPayload(meta=_meta(), amount_cents=4200,
                                 budget_remaining_cents=4995800, department="engineering-2026Q2")
    bad = PaymentFailedPayload(meta=_meta(), reason="insufficient_budget", compensated=False)
    assert PaymentCompletedPayload.model_validate(ok.model_dump()) == ok
    assert PaymentFailedPayload.model_validate(bad.model_dump()) == bad
