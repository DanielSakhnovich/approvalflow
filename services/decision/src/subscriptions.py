"""
Dapr pub/sub subscriptions for the decision service (Task 7).

Always-ack semantics identical to intake's (a duplicate/redelivered event is
acked, not retried) with one deliberate exception: if handling actually
raises -- the state store is unreachable, `publish()` fails, etc. -- nothing
here catches it. decision-svc's business logic has no "acked no-op" outcome
the way intake's illegal-transition handling does (a duplicate invoice is
just a `route`, not an exception; every fixture routes to *something*), so
any exception that does reach these handlers is, by construction, a genuine
infrastructure failure -- and FastAPI's default unhandled-exception behavior
turns that into a 500, exactly what Dapr needs to redeliver instead of
silently losing the event.
"""

import logging

from afcommon.contracts import InvoiceSubmittedPayload, PaymentCompletedPayload
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import PUBSUB_NAME, TOPIC_INVOICE_SUBMITTED, TOPIC_PAYMENT_COMPLETED
from fastapi import APIRouter, Depends

from .deps import get_dedupe, get_pipeline, get_trust
from .pipeline import DecisionPipeline
from .trust import TrustRepo

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}
_TOPICS = [TOPIC_INVOICE_SUBMITTED, TOPIC_PAYMENT_COMPLETED]


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"} for t in _TOPICS]


@router.post("/events/" + TOPIC_INVOICE_SUBMITTED)
async def on_invoice_submitted(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    pipeline: DecisionPipeline = Depends(get_pipeline),
) -> dict:
    payload = parse_cloudevent(InvoiceSubmittedPayload, event)
    bind_event_context(payload.meta)
    if not await dedupe.first_time(payload.meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    await pipeline.handle_submission(payload)
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_COMPLETED)
async def on_payment_completed(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    trust: TrustRepo = Depends(get_trust),
) -> dict:
    payload = parse_cloudevent(PaymentCompletedPayload, event)
    bind_event_context(payload.meta)
    if not await dedupe.first_time(payload.meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    await trust.record_paid_for_invoice(payload.meta.invoice_id)
    return _ACK
