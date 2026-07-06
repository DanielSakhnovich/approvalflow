"""
Dapr pub/sub subscriber for the approval service (Task 2 / Phase 04).

`decision-made` is the only inbound topic. Only the `human_review` route
ever needs a human -- `auto_approve`/`reject`/`duplicate` are already
final decisions made upstream by decision-svc's router and never surface
here at all. So this handler's first move is a route filter: anything
other than `human_review` is acked immediately, before dedupe is even
consulted.

That ordering is deliberate, not incidental: `EventDedupe.first_time()`
marks `processed:{event_id}` the moment it's called, and that mark is
irreversible from the caller's point of view except via the explicit
`forget()` compensation below. Marking it for a route this handler was
never going to act on anyway would burn dedupe budget for no reason --
and more importantly, would make the filter's own correctness untestable
from the outside, since a route this service doesn't process shouldn't be
able to "use up" the one-shot processing guarantee for that event_id. So
the filter runs first: non-`human_review` routes never touch the dedupe
store at all.

Once a `human_review` route clears the filter, this follows the same
mark -> process -> forget-on-exception -> re-raise shape as decision-svc's
subscriber (services/decision/src/subscriptions.py, D-016): `first_time()`
marks the event before `Escalation.from_decision` + `save_new` +
`add_to_queue` run, and any exception in that body triggers a best-effort
`dedupe.forget()` (itself guarded -- logged, not swallowed, if the forget
also fails) before re-raising, so Dapr's resulting 500 leads to a
redelivery that actually reprocesses the event instead of silently
hitting a stale mark.

`save_new` returning False means this exact invoice already has an
escalation record -- e.g. a second, distinct decision-made event (a
different event_id) for an invoice that's already pending or resolved.
That's a legitimate business outcome, not a failure: it's logged and
acked, and the pending queue is left untouched (no redundant
`add_to_queue` call).
"""

import logging

from afcommon.contracts import DecisionMadePayload
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import PUBSUB_NAME, TOPIC_DECISION_MADE
from fastapi import APIRouter, Depends

from .deps import get_dedupe, get_repo
from .models import Escalation
from .repo import ApprovalRepo

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}
_TOPICS = [TOPIC_DECISION_MADE]
_ESCALATION_ROUTE = "human_review"


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"} for t in _TOPICS]


async def _compensate_forget(dedupe: EventDedupe, event_id: str) -> None:
    """Best-effort unmark so Dapr's redelivery of `event_id` reprocesses
    instead of silently hitting a stale dedupe mark (see module docstring)."""
    try:
        await dedupe.forget(event_id)
    except Exception:
        log.exception("dedupe.forget failed for event_id=%s; mark may persist", event_id)


@router.post("/events/" + TOPIC_DECISION_MADE)
async def on_decision_made(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    repo: ApprovalRepo = Depends(get_repo),
) -> dict:
    payload = parse_cloudevent(DecisionMadePayload, event)
    bind_event_context(payload.meta)

    if payload.route != _ESCALATION_ROUTE:
        log.info("route=%s is not human_review; acked without dedupe", payload.route)
        return _ACK

    event_id = payload.meta.event_id
    if not await dedupe.first_time(event_id):
        log.info("duplicate event skipped")
        return _ACK

    try:
        escalation = Escalation.from_decision(payload)
        created = await repo.save_new(escalation)
        if not created:
            log.info(
                "escalation already exists for invoice_id=%s; acked",
                payload.meta.invoice_id,
            )
            return _ACK
        await repo.add_to_queue(payload.meta.invoice_id)
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK
