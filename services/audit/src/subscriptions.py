"""Dapr pub/sub subscriptions for the audit service.

Audit is the immutable record of the whole system: it subscribes to ALL five
topics and appends every event to `trail:{correlation_id}` (F9), NEVER
filtering by route or verdict -- a decision that was rejected, an escalation
that was sent back, a payment that failed all belong in the trail. It is the
one consumer that is deliberately un-opinionated about content.

Dedupe/forget follows the decision-svc pattern (D-016): mark
`processed:{event_id}` before appending; if the append raises, forget the
mark so Dapr's redelivery reprocesses instead of hitting a stale mark.

Carry from Phase 05: payment publishes its terminal events AFTER writing the
saga marker, so a crash between marker and publish can drop a
payment-completed/failed. The trail is append-only, so a missing event is a
GAP, never a corruption -- audit tolerates gaps and does not assume
exactly-once receipt.

The handler makes TWO writes for an auto-approve decision (the trail entry
and the ceiling index), so a failure of the second after the first succeeds,
followed by forget+redelivery, would replay the first write. Both
AuditTrail.append (idempotent by event_id) and append_auto_approval
(idempotent by invoice_id) are therefore safe no-ops on replay -- the same
idempotent-by-content discipline approval-svc uses for its save_new +
add_to_queue pair -- so redelivery never double-records.
"""

import logging

from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    InvoiceSubmittedPayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import (
    PUBSUB_NAME,
    TOPIC_APPROVAL_RESOLVED,
    TOPIC_DECISION_MADE,
    TOPIC_INVOICE_SUBMITTED,
    TOPIC_PAYMENT_COMPLETED,
    TOPIC_PAYMENT_FAILED,
)
from fastapi import APIRouter, Depends

from .deps import get_dedupe, get_trail
from .trail import AuditTrail, TrailEntry

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}

# topic -> the payload model to validate the CloudEvent data into.
_MODELS = {
    TOPIC_INVOICE_SUBMITTED: InvoiceSubmittedPayload,
    TOPIC_DECISION_MADE: DecisionMadePayload,
    TOPIC_APPROVAL_RESOLVED: ApprovalResolvedPayload,
    TOPIC_PAYMENT_COMPLETED: PaymentCompletedPayload,
    TOPIC_PAYMENT_FAILED: PaymentFailedPayload,
}


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"}
            for t in _MODELS]


async def _compensate_forget(dedupe: EventDedupe, event_id: str) -> None:
    try:
        await dedupe.forget(event_id)
    except Exception:
        log.exception("dedupe.forget failed for event_id=%s; mark may persist", event_id)


async def _record(topic: str, event: dict, trail: AuditTrail, dedupe: EventDedupe) -> dict:
    payload = parse_cloudevent(_MODELS[topic], event)
    meta = payload.meta
    bind_event_context(meta)
    if not await dedupe.first_time(meta.event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        entry = TrailEntry(
            event_type=topic,
            event_id=meta.event_id,
            occurred_at=meta.occurred_at,
            payload=payload.model_dump(),
        )
        await trail.append(meta.correlation_id, entry)
        # F10 projection: index auto-approvals so ceiling compliance is queryable.
        if topic == TOPIC_DECISION_MADE and payload.route == "auto_approve":
            await trail.append_auto_approval({
                "correlation_id": meta.correlation_id,
                "invoice_id": meta.invoice_id,
                "usd_cents": payload.usd_cents,
                "ceiling_cents": payload.ceiling_cents,
            })
    except Exception:
        await _compensate_forget(dedupe, meta.event_id)
        raise
    return _ACK


@router.post("/events/" + TOPIC_INVOICE_SUBMITTED)
async def on_invoice_submitted(event: dict, trail: AuditTrail = Depends(get_trail),
                               dedupe: EventDedupe = Depends(get_dedupe)) -> dict:
    return await _record(TOPIC_INVOICE_SUBMITTED, event, trail, dedupe)


@router.post("/events/" + TOPIC_DECISION_MADE)
async def on_decision_made(event: dict, trail: AuditTrail = Depends(get_trail),
                           dedupe: EventDedupe = Depends(get_dedupe)) -> dict:
    return await _record(TOPIC_DECISION_MADE, event, trail, dedupe)


@router.post("/events/" + TOPIC_APPROVAL_RESOLVED)
async def on_approval_resolved(event: dict, trail: AuditTrail = Depends(get_trail),
                               dedupe: EventDedupe = Depends(get_dedupe)) -> dict:
    return await _record(TOPIC_APPROVAL_RESOLVED, event, trail, dedupe)


@router.post("/events/" + TOPIC_PAYMENT_COMPLETED)
async def on_payment_completed(event: dict, trail: AuditTrail = Depends(get_trail),
                               dedupe: EventDedupe = Depends(get_dedupe)) -> dict:
    return await _record(TOPIC_PAYMENT_COMPLETED, event, trail, dedupe)


@router.post("/events/" + TOPIC_PAYMENT_FAILED)
async def on_payment_failed(event: dict, trail: AuditTrail = Depends(get_trail),
                            dedupe: EventDedupe = Depends(get_dedupe)) -> dict:
    return await _record(TOPIC_PAYMENT_FAILED, event, trail, dedupe)
