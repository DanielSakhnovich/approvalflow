from enum import StrEnum

from afcommon.contracts import DecisionMadePayload
from pydantic import BaseModel


class EscalationStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    needs_info = "needs_info"


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
        payload is all Phase 04 has.
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
            escalated_at=payload.meta.occurred_at,
        )
