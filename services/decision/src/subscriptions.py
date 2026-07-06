"""
Dapr pub/sub subscriptions for the decision service (Task 7).

Always-ack semantics identical to intake's (a duplicate/redelivered event is
acked, not retried) with one deliberate exception: if handling actually
raises -- the state store is unreachable, `publish()` fails, etc. --
decision-svc's business logic has no "acked no-op" outcome the way intake's
illegal-transition handling does (a duplicate invoice is just a `route`, not
an exception; every fixture routes to *something*), so any exception that
reaches these handlers is, by construction, a genuine infrastructure
failure -- and FastAPI's default unhandled-exception behavior turns that
into a 500, which is what Dapr needs in order to redeliver instead of
silently losing the event.

That 500-for-redelivery story only holds together, though, if the
redelivered copy actually gets reprocessed. Both handlers below mark
`processed:{event_id}` (via `EventDedupe.first_time`, D-016) *before*
running the handler body, so if the body then raises -- e.g. the state
store is healthy but the downstream `publish(decision-made)` call fails --
the mark is already persisted. Left alone, that would silently lose the
event forever: Dapr redelivers on the 500, but the redelivered copy hits the
stale mark and gets acked as "duplicate", with no second attempt ever made.
So the post-mark body is wrapped in a try/except: on any exception, a
best-effort `dedupe.forget(event_id)` undoes the mark (itself guarded --
logged, not swallowed, if the forget also fails) before re-raising, so the
500 still propagates. With the mark undone, the redelivery reprocesses the
event for real.

Residual gap: a hard crash (process killed between the mark and the
forget -- e.g. OOM-killed, SIGKILL) leaves the mark in place with no
compensation running at all. That window can't be closed from inside a
single handler and is accepted as-is; it's also low-consequence here,
since downstream consumers are idempotent per-invoice (intake's status
transitions are CAS-guarded; the payment saga is keyed by invoice_id), so
even a rare double-publish slipping through elsewhere is absorbed rather
than corrupting state.
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


async def _compensate_forget(dedupe: EventDedupe, event_id: str) -> None:
    """Best-effort unmark so Dapr's redelivery of `event_id` reprocesses
    instead of silently hitting a stale dedupe mark (see module docstring).
    Failure to forget is logged, not swallowed: a store too broken to
    delete right now is very likely the same store that failed to persist
    the original mark, but that's an assumption worth logging honestly
    rather than making silently."""
    try:
        await dedupe.forget(event_id)
    except Exception:
        log.exception("dedupe.forget failed for event_id=%s; mark may persist", event_id)


@router.post("/events/" + TOPIC_INVOICE_SUBMITTED)
async def on_invoice_submitted(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    pipeline: DecisionPipeline = Depends(get_pipeline),
) -> dict:
    payload = parse_cloudevent(InvoiceSubmittedPayload, event)
    bind_event_context(payload.meta)
    event_id = payload.meta.event_id
    if not await dedupe.first_time(event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        await pipeline.handle_submission(payload)
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK


@router.post("/events/" + TOPIC_PAYMENT_COMPLETED)
async def on_payment_completed(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    trust: TrustRepo = Depends(get_trust),
) -> dict:
    payload = parse_cloudevent(PaymentCompletedPayload, event)
    bind_event_context(payload.meta)
    event_id = payload.meta.event_id
    if not await dedupe.first_time(event_id):
        log.info("duplicate event skipped")
        return _ACK
    try:
        await trust.record_paid_for_invoice(payload.meta.invoice_id)
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK
