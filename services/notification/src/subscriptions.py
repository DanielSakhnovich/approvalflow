"""Dapr pub/sub subscriptions for the notification service (D-006).

Reuses the HW3 notification domain (storage/processor/segmenter/providers)
unchanged; this module is only the thin transport that turns ApprovalFlow
outcome events into submitter-facing notifications and hands them to the
vendored processor for delivery through the mock providers.

Notify only on outcomes the submitter should hear about:
  - decision-made with route human_review (paused for a person) or reject
  - payment-completed / payment-failed (the terminal result)
An auto_approve decision is NOT notified on its own -- the terminal payment
event carries that outcome, so notifying on both would double-message.

Dedupe/forget follows the D-016 pattern: mark before delivering; forget on
failure so Dapr's redelivery reprocesses.
"""

import logging

from afcommon.contracts import (
    DecisionMadePayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import (
    PUBSUB_NAME,
    TOPIC_DECISION_MADE,
    TOPIC_PAYMENT_COMPLETED,
    TOPIC_PAYMENT_FAILED,
)
from fastapi import APIRouter, Depends

from . import storage
from .deps import get_dedupe, get_processor
from .processor import NotificationProcessor

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}
_TOPICS = [TOPIC_DECISION_MADE, TOPIC_PAYMENT_COMPLETED, TOPIC_PAYMENT_FAILED]

# Placeholder recipient: the wire payloads don't carry the submitter's contact,
# so we notify a per-invoice email channel. Real contact resolution would be a
# lookup; out of scope for the M-tier (documented).
def _channel_for(invoice_id: str) -> list[dict]:
    return [{"type": "email", "value": f"submitter+{invoice_id}@northwind.example"}]


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"}
            for t in _TOPICS]


async def _compensate_forget(dedupe: EventDedupe, event_id: str) -> None:
    try:
        await dedupe.forget(event_id)
    except Exception:
        log.exception("dedupe.forget failed for event_id=%s; mark may persist", event_id)


async def _notify(invoice_id: str, message: str,
                  processor: NotificationProcessor) -> None:
    n = storage.add_notification(_channel_for(invoice_id), message)
    await processor.send_one(n)


@router.post("/events/" + TOPIC_DECISION_MADE)
async def on_decision_made(event: dict,
                           dedupe: EventDedupe = Depends(get_dedupe),
                           processor: NotificationProcessor = Depends(get_processor)) -> dict:
    payload = parse_cloudevent(DecisionMadePayload, event)
    bind_event_context(payload.meta)
    if payload.route not in ("human_review", "reject"):
        return _ACK  # auto_approve/duplicate: terminal payment event covers it
    if not await dedupe.first_time(payload.meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        verb = "needs your review" if payload.route == "human_review" else "was rejected"
        await _notify(payload.meta.invoice_id,
                      f"Your submission {verb}: {payload.reasoning}", processor)
    except Exception:
        await _compensate_forget(dedupe, payload.meta.event_id)
        raise
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_COMPLETED)
async def on_payment_completed(event: dict,
                               dedupe: EventDedupe = Depends(get_dedupe),
                               processor: NotificationProcessor = Depends(get_processor)) -> dict:
    payload = parse_cloudevent(PaymentCompletedPayload, event)
    bind_event_context(payload.meta)
    if not await dedupe.first_time(payload.meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        await _notify(payload.meta.invoice_id,
                      f"Your expense was paid: {payload.amount_cents} cents.", processor)
    except Exception:
        await _compensate_forget(dedupe, payload.meta.event_id)
        raise
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_FAILED)
async def on_payment_failed(event: dict,
                            dedupe: EventDedupe = Depends(get_dedupe),
                            processor: NotificationProcessor = Depends(get_processor)) -> dict:
    payload = parse_cloudevent(PaymentFailedPayload, event)
    bind_event_context(payload.meta)
    if not await dedupe.first_time(payload.meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        await _notify(payload.meta.invoice_id,
                      f"Your expense payment did not complete: {payload.reason}.", processor)
    except Exception:
        await _compensate_forget(dedupe, payload.meta.event_id)
        raise
    return _ACK
