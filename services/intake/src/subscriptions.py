import logging
from typing import Any

from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    InvoiceSubmittedPayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.events import (
    PUBSUB_NAME,
    TOPIC_APPROVAL_RESOLVED,
    TOPIC_DECISION_MADE,
    TOPIC_INVOICE_SUBMITTED,
    TOPIC_PAYMENT_COMPLETED,
    TOPIC_PAYMENT_FAILED,
)
from afcommon.logging import correlation_id_var, invoice_id_var
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .deps import get_repo
from .models import InvoiceRecord
from .repo import IntakeRepo
from .status import (
    IllegalTransition,
    apply_approval,
    apply_decision,
    apply_payment_completed,
    apply_payment_failed,
)

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}

_TOPICS = [
    TOPIC_INVOICE_SUBMITTED, TOPIC_DECISION_MADE, TOPIC_APPROVAL_RESOLVED,
    TOPIC_PAYMENT_COMPLETED, TOPIC_PAYMENT_FAILED,
]


class CloudEvent(BaseModel):
    data: dict[str, Any]


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"}
            for t in _TOPICS]


async def _handle(repo: IntakeRepo, payload, apply_fn) -> InvoiceRecord | None:
    """Dedupe, then CAS-transition the record. Returns the updated record on
    success, or None if the event was a no-op (duplicate, unknown invoice, or
    an illegal/out-of-order transition) - callers must still ack in all cases.
    """
    meta = payload.meta
    invoice_id_var.set(meta.invoice_id)
    correlation_id_var.set(meta.correlation_id)
    if not await repo.first_time_event(meta.event_id):
        log.info("duplicate event skipped")
        return None
    try:
        return await repo.update_record(meta.invoice_id, lambda rec: apply_fn(rec, payload))
    except KeyError:
        log.warning("event for unknown invoice; acked")
        return None
    except IllegalTransition as exc:
        log.warning("illegal transition acked: %s", exc)
        return None


@router.post("/events/" + TOPIC_INVOICE_SUBMITTED)
async def on_invoice_submitted(event: CloudEvent,
                               repo: IntakeRepo = Depends(get_repo)) -> dict:
    payload = InvoiceSubmittedPayload.model_validate(event.data)
    await repo.first_time_event(payload.meta.event_id)
    return _ACK  # intake already updated its own record at POST time


@router.post("/events/" + TOPIC_DECISION_MADE)
async def on_decision_made(event: CloudEvent,
                           repo: IntakeRepo = Depends(get_repo)) -> dict:
    payload = DecisionMadePayload.model_validate(event.data)
    updated = await _handle(repo, payload, apply_decision)
    if updated is not None:
        await repo.bump_counters(**{f"decided_{payload.route}": 1})
    return _ACK


@router.post("/events/" + TOPIC_APPROVAL_RESOLVED)
async def on_approval_resolved(event: CloudEvent,
                               repo: IntakeRepo = Depends(get_repo)) -> dict:
    payload = ApprovalResolvedPayload.model_validate(event.data)
    updated = await _handle(repo, payload, apply_approval)
    if updated is not None:
        await repo.bump_counters(**{f"verdict_{payload.verdict}": 1})
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_COMPLETED)
async def on_payment_completed(event: CloudEvent,
                               repo: IntakeRepo = Depends(get_repo)) -> dict:
    payload = PaymentCompletedPayload.model_validate(event.data)
    updated = await _handle(repo, payload, apply_payment_completed)
    if updated is not None:
        bucket = "paid_auto_cents" if updated.decided_by == "router" else "paid_human_cents"
        await repo.bump_counters(**{"paid": 1, bucket: payload.amount_cents})
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_FAILED)
async def on_payment_failed(event: CloudEvent,
                            repo: IntakeRepo = Depends(get_repo)) -> dict:
    payload = PaymentFailedPayload.model_validate(event.data)
    updated = await _handle(repo, payload, apply_payment_failed)
    if updated is not None:
        await repo.bump_counters(payment_failed=1)
    return _ACK
