"""
Dapr pub/sub subscriber for the payment service (Task 4, Phase 05).

Two inbound topics feed the same `PaymentSaga`, each behind its own route
filter:

- `decision-made`: only the `auto_approve` route is a real trigger for
  payment -- `human_review`/`reject`/`duplicate` are decision-svc routing
  outcomes this service has nothing to do with (a `human_review` route
  eventually produces its own `approval-resolved` event once a human acts,
  which is the OTHER topic below). So `on_decision_made` acks every
  non-`auto_approve` route immediately.
- `approval-resolved`: only the `approved` verdict authorizes payment --
  `rejected`/`needs_info` are terminal-for-payment (rejected) or not yet
  resolved (needs_info loops back through decision-svc, not payment). So
  `on_approval_resolved` acks every non-`approved` verdict immediately.

Both filters run BEFORE `EventDedupe.first_time()`, mirroring approval-svc's
`decision-made` subscriber (services/approval/src/subscriptions.py, T2):
`first_time()` marks `processed:{event_id}` irreversibly (short of the
explicit `forget()` compensation below), so a route/verdict this handler was
never going to act on must not burn dedupe budget for that event_id -- and,
just as importantly, a filtered-out event must stay externally
indistinguishable from "never delivered" for `event_id` reuse.

Once a payload clears its filter, both handlers follow the same
mark -> process -> forget-on-exception -> re-raise shape as decision-svc's
and approval-svc's subscribers: `first_time()` marks the event, then
`saga.handle(...)` runs, and any exception in that body triggers a
best-effort `dedupe.forget()` (logged, not swallowed, if the forget itself
fails) before re-raising, so Dapr's resulting 500 leads to a redelivery
that actually reprocesses the event instead of silently hitting a stale
mark.

Dedupe here only protects per-EVENT_ID redelivery (Dapr's own retries of
the exact same message). `PaymentSaga.handle()` carries its own, separate
per-INVOICE_ID idempotency (a durable `saga:{invoice_id}` record short-
circuits at any terminal state) -- that's what keeps a genuinely distinct
SECOND `approval-resolved` event for an invoice that's already paid (e.g. a
duplicate verdict submission, a fresh event_id from a publish-retry) from
ever triggering a second payment: dedupe would happily mark that fresh
event_id as first-time, but `saga.handle()` sees the already-terminal
record and returns it unchanged, no re-reserve/re-execute/re-publish.

Missing/empty `department` (a decision-made or approval-resolved payload
that never got a department attached) is NOT special-cased here: it's
handled entirely by `BudgetStore.reserve`, which returns `False` for an
unknown department key, so `saga.handle()` naturally resolves to
`rejected_insufficient_budget` -- a normal saga outcome, not a crash path.
"""

import logging

from afcommon.contracts import ApprovalResolvedPayload, DecisionMadePayload
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import PUBSUB_NAME, TOPIC_APPROVAL_RESOLVED, TOPIC_DECISION_MADE
from fastapi import APIRouter, Depends

from .deps import get_dedupe, get_saga
from .saga import PaymentSaga

log = logging.getLogger(__name__)
router = APIRouter()

_ACK = {"status": "SUCCESS"}
_TOPICS = [TOPIC_DECISION_MADE, TOPIC_APPROVAL_RESOLVED]
_AUTO_APPROVE_ROUTE = "auto_approve"
_APPROVED_VERDICT = "approved"


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
    saga: PaymentSaga = Depends(get_saga),
) -> dict:
    payload = parse_cloudevent(DecisionMadePayload, event)
    bind_event_context(payload.meta)

    if payload.route != _AUTO_APPROVE_ROUTE:
        log.info("route=%s is not auto_approve; acked without dedupe", payload.route)
        return _ACK

    event_id = payload.meta.event_id
    if not await dedupe.first_time(event_id):
        log.info("duplicate event skipped")
        return _ACK

    try:
        await saga.handle(
            invoice_id=payload.meta.invoice_id,
            correlation_id=payload.meta.correlation_id,
            department=payload.department,
            amount_cents=payload.usd_cents,
            scenario=payload.scenario,
            auto_route=True,
            ceiling_cents=payload.ceiling_cents,
        )
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK


@router.post("/events/" + TOPIC_APPROVAL_RESOLVED)
async def on_approval_resolved(
    event: dict,
    dedupe: EventDedupe = Depends(get_dedupe),
    saga: PaymentSaga = Depends(get_saga),
) -> dict:
    payload = parse_cloudevent(ApprovalResolvedPayload, event)
    bind_event_context(payload.meta)

    if payload.verdict != _APPROVED_VERDICT:
        log.info("verdict=%s is not approved; acked without dedupe", payload.verdict)
        return _ACK

    event_id = payload.meta.event_id
    if not await dedupe.first_time(event_id):
        log.info("duplicate event skipped")
        return _ACK

    try:
        await saga.handle(
            invoice_id=payload.meta.invoice_id,
            correlation_id=payload.meta.correlation_id,
            department=payload.department,
            amount_cents=payload.usd_cents,
            scenario=payload.scenario,
            auto_route=False,
            ceiling_cents=None,
        )
    except Exception:
        await _compensate_forget(dedupe, event_id)
        raise
    return _ACK
