from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)

from .models import InvoiceRecord, InvoiceStatus, touch


class IllegalTransition(Exception):
    """Event does not apply to the record's current status."""


def _require(record: InvoiceRecord, expected: InvoiceStatus, event: str) -> None:
    if record.status != expected:
        raise IllegalTransition(
            f"{event} not applicable: {record.invoice_id} is {record.status}, "
            f"expected {expected}")


_DECISION_TARGET = {
    "auto_approve": InvoiceStatus.APPROVED,
    "human_review": InvoiceStatus.PENDING_APPROVAL,
    "reject": InvoiceStatus.REJECTED,
    "duplicate": InvoiceStatus.DUPLICATE,
}


def apply_decision(record: InvoiceRecord, p: DecisionMadePayload) -> InvoiceRecord:
    _require(record, InvoiceStatus.EVALUATING, "decision-made")
    decided_by = None if p.route == "human_review" else "router"
    return touch(record, status=_DECISION_TARGET[p.route], route=p.route,
                  reasoning=p.reasoning, decided_by=decided_by)


_VERDICT_TARGET = {
    "approved": InvoiceStatus.APPROVED,
    "rejected": InvoiceStatus.REJECTED,
    "needs_info": InvoiceStatus.NEEDS_INFO,
}


def apply_approval(record: InvoiceRecord, p: ApprovalResolvedPayload) -> InvoiceRecord:
    _require(record, InvoiceStatus.PENDING_APPROVAL, "approval-resolved")
    decided_by = record.decided_by if p.verdict == "needs_info" else p.approver_id
    if p.comment:
        reasoning = f"{record.reasoning}\nApprover: {p.comment}".strip()
    else:
        reasoning = record.reasoning
    return touch(record, status=_VERDICT_TARGET[p.verdict],
                  decided_by=decided_by, reasoning=reasoning)


def apply_payment_completed(record: InvoiceRecord, p: PaymentCompletedPayload) -> InvoiceRecord:
    _require(record, InvoiceStatus.APPROVED, "payment-completed")
    return touch(record, status=InvoiceStatus.PAID)


def apply_payment_failed(record: InvoiceRecord, p: PaymentFailedPayload) -> InvoiceRecord:
    _require(record, InvoiceStatus.APPROVED, "payment-failed")
    reasoning = f"{record.reasoning}\nPayment failed: {p.reason}".strip()
    return touch(record, status=InvoiceStatus.PAYMENT_FAILED, reasoning=reasoning)
