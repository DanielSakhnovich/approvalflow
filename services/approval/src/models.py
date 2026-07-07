from enum import StrEnum

from afcommon.contracts import DecisionMadePayload
from pydantic import BaseModel


class EscalationStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    needs_info = "needs_info"


class AlreadyResolved(Exception):
    """Raised inside an `ApprovalRepo.resolve()` CAS transform when the
    escalation's status is already something other than pending. Carries
    the existing (already-resolved) escalation so the caller can hand back
    an idempotent-read-back of who/when/what resolved it, instead of a bare
    conflict."""

    def __init__(self, escalation: "Escalation"):
        super().__init__(
            f"escalation {escalation.invoice_id} already resolved as {escalation.status}")
        self.escalation = escalation


class Escalation(BaseModel):
    invoice_id: str
    correlation_id: str
    status: EscalationStatus
    usd_cents: int
    route_violations: list[str]
    recommendation: str
    confidence: float | None
    reasoning: str
    submitter: str = ""
    vendor: str = ""
    category: str = ""
    # Harness marker for fixture-driven failure injection (Phase 05's payment
    # provider gate), carried through from the decision-made payload.
    scenario: str = ""
    # Budget owner for payment (Phase 05), carried through from the
    # decision-made payload.
    department: str = ""
    escalated_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution_comment: str = ""

    @classmethod
    def from_decision(
        cls, payload: DecisionMadePayload, invoice: dict | None = None
    ) -> "Escalation":
        """Create an Escalation from a DecisionMadePayload.

        The decision-made payload carries no submission context, so vendor/submitter/
        category default to empty strings. Passing the original invoice dict (fixture
        shape) enriches those three display fields; no caller does so yet — the wire
        payload is all Phase 04 has. `scenario`/`department`, in contrast, ride the
        wire payload itself (populated by decision-svc's pipeline from the invoice),
        so they're mapped straight from `payload`, not from the optional `invoice` arg.
        """
        invoice = invoice or {}
        return cls(
            invoice_id=payload.meta.invoice_id,
            correlation_id=payload.meta.correlation_id,
            status=EscalationStatus.pending,
            usd_cents=payload.usd_cents,
            route_violations=payload.violations,
            recommendation=payload.recommendation,
            confidence=payload.confidence,
            reasoning=payload.reasoning,
            submitter=str(invoice.get("submitter", "") or ""),
            vendor=str(invoice.get("vendor", "") or ""),
            category=str(invoice.get("category", "") or ""),
            scenario=payload.scenario,
            department=payload.department,
            escalated_at=payload.meta.occurred_at,
        )
