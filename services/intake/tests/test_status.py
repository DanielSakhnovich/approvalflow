import pytest
from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.events import new_event_meta

from services.intake.src.models import InvoiceRecord, InvoiceStatus
from services.intake.src.status import (
    IllegalTransition,
    apply_approval,
    apply_decision,
    apply_payment_completed,
    apply_payment_failed,
)


def rec(status: InvoiceStatus) -> InvoiceRecord:
    return InvoiceRecord(
        invoice_id="inv_1", client_ref="INV-1001", correlation_id="corr_1",
        status=status, submission={}, created_at="t", updated_at="t",
    )


def decision(route: str) -> DecisionMadePayload:
    return DecisionMadePayload(
        meta=new_event_meta("inv_1", "corr_1"), route=route, recommendation="x",
        confidence=0.9, violations=[], reasoning="because", usd_cents=1, ceiling_cents=2,
    )


def approval(verdict: str) -> ApprovalResolvedPayload:
    return ApprovalResolvedPayload(
        meta=new_event_meta("inv_1", "corr_1"), verdict=verdict,
        approver_id="lena@northwind.example", comment="checked",
    )


@pytest.mark.parametrize("route,expected", [
    ("auto_approve", InvoiceStatus.APPROVED),
    ("human_review", InvoiceStatus.PENDING_APPROVAL),
    ("reject", InvoiceStatus.REJECTED),
    ("duplicate", InvoiceStatus.DUPLICATE),
])
def test_decision_transitions(route, expected):
    out = apply_decision(rec(InvoiceStatus.EVALUATING), decision(route))
    assert out.status == expected
    assert out.route == route and out.reasoning == "because"
    if route != "human_review":
        assert out.decided_by == "router"


@pytest.mark.parametrize("verdict,expected", [
    ("approved", InvoiceStatus.APPROVED),
    ("rejected", InvoiceStatus.REJECTED),
    ("needs_info", InvoiceStatus.NEEDS_INFO),
])
def test_approval_transitions(verdict, expected):
    out = apply_approval(rec(InvoiceStatus.PENDING_APPROVAL), approval(verdict))
    assert out.status == expected
    if verdict != "needs_info":
        assert out.decided_by == "lena@northwind.example"
    assert "checked" in out.reasoning


def test_payment_transitions():
    ok = apply_payment_completed(rec(InvoiceStatus.APPROVED), PaymentCompletedPayload(
        meta=new_event_meta("inv_1", "corr_1"), amount_cents=1,
        budget_remaining_cents=1, department="d"))
    assert ok.status == InvoiceStatus.PAID
    bad = apply_payment_failed(rec(InvoiceStatus.APPROVED), PaymentFailedPayload(
        meta=new_event_meta("inv_1", "corr_1"), reason="insufficient_budget", compensated=True))
    assert bad.status == InvoiceStatus.PAYMENT_FAILED
    assert "insufficient_budget" in bad.reasoning


@pytest.mark.parametrize("fn,record_status,payload", [
    (apply_decision, InvoiceStatus.PAID, decision("auto_approve")),
    (apply_approval, InvoiceStatus.EVALUATING, approval("approved")),
    (apply_payment_completed, InvoiceStatus.PENDING_APPROVAL, PaymentCompletedPayload(
        meta=new_event_meta("inv_1", "corr_1"), amount_cents=1,
        budget_remaining_cents=1, department="d")),
])
def test_illegal_transitions_raise(fn, record_status, payload):
    with pytest.raises(IllegalTransition):
        fn(rec(record_status), payload)


def test_apply_does_not_mutate_original():
    original = rec(InvoiceStatus.EVALUATING)
    apply_decision(original, decision("reject"))
    assert original.status == InvoiceStatus.EVALUATING
