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
escalation record -- either a second, distinct decision-made event (a
different event_id) for an invoice that's already escalated, OR the
redelivery of an event whose first attempt failed *between* `save_new`
and `add_to_queue` (record durably written, queue write lost). Those two
cases are indistinguishable at the `save_new` return value, so the
handler disambiguates on the existing record's status:

- `pending`: `add_to_queue` is called anyway -- it's idempotent, so the
  legitimate already-queued case is a harmless no-op, while the
  partial-failure case gets the invoice back in front of approvers instead
  of being acked into permanent invisibility.
- `needs_info`: NOT terminal. Send-back is designed to loop -- an approver
  asks for more info, intake resubmits, decision-svc re-evaluates and may
  route `human_review` again with a fresh event_id. Treating that fresh
  event as "already exists, ack" would freeze the record in `needs_info`
  forever (verdict POSTs would 409 against a review that's already done).
  So this reopens the record: a CAS transform flips it back to `pending`,
  clears the resolved_* fields, and refreshes the decision fields
  (usd_cents/route_violations/recommendation/confidence/reasoning/
  escalated_at) from THIS payload -- then it's re-queued like any other
  pending escalation. If the CAS loses a race (the record moved off
  needs_info between the `get` and the transform), the transform raises
  `AlreadyResolved` and the handler just acks -- nothing to reopen.
- `approved`/`rejected`: genuinely terminal. Human review is done and it
  must not reappear, so this acks without queueing.
"""

import logging
from collections.abc import Callable

from afcommon.contracts import DecisionMadePayload
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import PUBSUB_NAME, TOPIC_DECISION_MADE
from fastapi import APIRouter, Depends

from .deps import get_dedupe, get_repo
from .models import AlreadyResolved, Escalation, EscalationStatus
from .repo import ApprovalRepo

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}
_TOPICS = [TOPIC_DECISION_MADE]
_ESCALATION_ROUTE = "human_review"


@router.get("/dapr/subscribe")
async def subscribe() -> list[dict]:
    return [{"pubsubname": PUBSUB_NAME, "topic": t, "route": f"/events/{t}"} for t in _TOPICS]


def _reopen_transform(payload: DecisionMadePayload) -> Callable[[Escalation], Escalation]:
    """Build the CAS transform that reopens a `needs_info` escalation into a
    fresh `pending` one, refreshed from `payload` (a new decision-made event
    for the same invoice, post send-back). Raises `AlreadyResolved` if the
    record is no longer `needs_info` by the time the transform runs (lost a
    race to another actor), so `repo.resolve`'s CAS bails instead of
    clobbering whatever it became."""

    def transform(esc: Escalation) -> Escalation:
        if esc.status != EscalationStatus.needs_info:
            raise AlreadyResolved(esc)
        return esc.model_copy(update={
            "status": EscalationStatus.pending,
            "usd_cents": payload.usd_cents,
            "route_violations": payload.violations,
            "recommendation": payload.recommendation,
            "confidence": payload.confidence,
            "reasoning": payload.reasoning,
            "escalated_at": payload.meta.occurred_at,
            "resolved_at": None,
            "resolved_by": None,
            "resolution_comment": "",
        })

    return transform


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
        invoice_id = payload.meta.invoice_id
        escalation = Escalation.from_decision(payload)
        created = await repo.save_new(escalation)
        if not created:
            # Record already exists: either a redundant decision-made for an
            # already-escalated invoice, or the redelivery of an event whose
            # first attempt died between save_new and add_to_queue. If it's
            # still pending, re-queue (idempotent) so a lost queue write
            # can't leave the escalation invisible to approvers forever.
            existing = await repo.get(invoice_id)
            if existing is not None and existing.status == EscalationStatus.pending:
                log.info(
                    "escalation already exists and is pending for invoice_id=%s; "
                    "re-queueing (idempotent) and acking",
                    invoice_id,
                )
                await repo.add_to_queue(invoice_id)
            elif existing is not None and existing.status == EscalationStatus.needs_info:
                try:
                    await repo.resolve(invoice_id, _reopen_transform(payload))
                    await repo.add_to_queue(invoice_id)
                    log.info(
                        "reopened needs_info escalation for invoice_id=%s back to "
                        "pending (refreshed from new decision-made) and re-queued",
                        invoice_id,
                    )
                except AlreadyResolved:
                    log.info(
                        "needs_info reopen raced for invoice_id=%s -- record moved "
                        "on before the CAS applied; acked without queueing",
                        invoice_id,
                    )
            else:
                log.info(
                    "escalation already resolved for invoice_id=%s; acked without queueing",
                    invoice_id,
                )
            return _ACK
        await repo.add_to_queue(invoice_id)
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK
