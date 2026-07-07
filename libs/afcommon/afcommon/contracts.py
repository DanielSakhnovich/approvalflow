from typing import Any, Literal

from pydantic import BaseModel

from .events import EventMeta

Route = Literal["auto_approve", "human_review", "reject", "duplicate"]
Verdict = Literal["approved", "rejected", "needs_info"]


class InvoiceSubmittedPayload(BaseModel):
    meta: EventMeta
    invoice: dict[str, Any]


class DecisionMadePayload(BaseModel):
    meta: EventMeta
    route: Route
    recommendation: str
    confidence: float | None
    violations: list[str]
    reasoning: str
    usd_cents: int
    ceiling_cents: int


class ApprovalResolvedPayload(BaseModel):
    """AT-LEAST-ONCE per invoice, fresh event_id per attempt (approval's
    publish-failure rollback + retry): consumers must be idempotent per
    invoice_id, not per event_id."""

    meta: EventMeta
    verdict: Verdict
    approver_id: str
    comment: str = ""
    # Amount in integer cents, carried so payment (Phase 05) can size the
    # budget reservation without re-querying. Defaults to 0 so existing
    # fixtures/tests that don't set it keep passing.
    usd_cents: int = 0


class PaymentCompletedPayload(BaseModel):
    meta: EventMeta
    amount_cents: int
    budget_remaining_cents: int
    department: str


class PaymentFailedPayload(BaseModel):
    meta: EventMeta
    reason: str
    compensated: bool
