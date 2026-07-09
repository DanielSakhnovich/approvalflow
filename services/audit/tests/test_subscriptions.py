"""Task 2: audit records EVERY event into the correlation trail (F9), never
filtering by route/verdict, and maintains the auto-approval ceiling index
(F10). Dedupe (D-016) prevents a redelivered event from double-appending;
a post-append failure forgets the mark so Dapr's redelivery reprocesses.
"""

from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    InvoiceSubmittedPayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.events import (
    TOPIC_APPROVAL_RESOLVED,
    TOPIC_DECISION_MADE,
    TOPIC_INVOICE_SUBMITTED,
    TOPIC_PAYMENT_COMPLETED,
    TOPIC_PAYMENT_FAILED,
    new_event_meta,
)
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.audit.src import deps
from services.audit.src.main import app
from services.audit.src.trail import AuditTrail


def cloudevent(payload: dict) -> dict:
    return {"data": payload}


def make_env():
    trail = AuditTrail(InMemoryStateStore())
    dedupe_store = InMemoryStateStore()
    from afcommon.dedupe import EventDedupe
    dedupe = EventDedupe(dedupe_store)
    app.dependency_overrides[deps.get_trail] = lambda: trail
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    return TestClient(app), trail


def teardown_function(_fn):
    app.dependency_overrides.clear()


def _submitted(cid="corr-1", eid="evt-sub", occ="2026-07-01T00:00:00Z"):
    return InvoiceSubmittedPayload(
        meta=new_event_meta("inv-1", cid).model_copy(
            update={"event_id": eid, "occurred_at": occ}),
        invoice={"id": "INV-1", "vendor": "Bistro 19"},
    ).model_dump()


def _decision(route="human_review", cid="corr-1", eid="evt-dec",
              occ="2026-07-01T00:01:00Z", usd=100, ceil=25000):
    return DecisionMadePayload(
        meta=new_event_meta("inv-1", cid).model_copy(
            update={"event_id": eid, "occurred_at": occ}),
        route=route, recommendation="approve", confidence=0.9, violations=[],
        reasoning="r", usd_cents=usd, ceiling_cents=ceil, department="engineering-2026Q2",
    ).model_dump()


def _approval(cid="corr-1", eid="evt-app", occ="2026-07-01T00:02:00Z"):
    return ApprovalResolvedPayload(
        meta=new_event_meta("inv-1", cid).model_copy(
            update={"event_id": eid, "occurred_at": occ}),
        verdict="approved", approver_id="lena", comment="ok",
        usd_cents=100, department="engineering-2026Q2",
    ).model_dump()


def _paid(cid="corr-1", eid="evt-pay", occ="2026-07-01T00:03:00Z"):
    return PaymentCompletedPayload(
        meta=new_event_meta("inv-1", cid).model_copy(
            update={"event_id": eid, "occurred_at": occ}),
        amount_cents=100, budget_remaining_cents=999900, department="engineering-2026Q2",
    ).model_dump()


def test_subscribe_lists_all_five_topics():
    client, _ = make_env()
    subs = client.get("/dapr/subscribe").json()
    topics = {s["topic"] for s in subs}
    assert topics == {
        TOPIC_INVOICE_SUBMITTED, TOPIC_DECISION_MADE, TOPIC_APPROVAL_RESOLVED,
        TOPIC_PAYMENT_COMPLETED, TOPIC_PAYMENT_FAILED,
    }
    assert all(s["pubsubname"] == "pubsub" for s in subs)


async def test_each_event_type_appends_a_typed_entry():
    client, trail = make_env()
    client.post(f"/events/{TOPIC_INVOICE_SUBMITTED}", json=cloudevent(_submitted()))
    entries = await trail.get_trail("corr-1")
    assert len(entries) == 1
    assert entries[0].event_type == TOPIC_INVOICE_SUBMITTED
    assert entries[0].payload["invoice"]["vendor"] == "Bistro 19"


async def test_full_journey_trail_is_ordered():
    client, trail = make_env()
    # deliver out of order; get_trail sorts by occurred_at
    client.post(f"/events/{TOPIC_PAYMENT_COMPLETED}", json=cloudevent(_paid()))
    client.post(f"/events/{TOPIC_INVOICE_SUBMITTED}", json=cloudevent(_submitted()))
    client.post(f"/events/{TOPIC_APPROVAL_RESOLVED}", json=cloudevent(_approval()))
    client.post(f"/events/{TOPIC_DECISION_MADE}", json=cloudevent(_decision()))
    entries = await trail.get_trail("corr-1")
    assert [e.event_type for e in entries] == [
        TOPIC_INVOICE_SUBMITTED, TOPIC_DECISION_MADE,
        TOPIC_APPROVAL_RESOLVED, TOPIC_PAYMENT_COMPLETED,
    ]


async def test_auto_approve_decision_also_indexed_for_ceiling():
    client, trail = make_env()
    client.post(f"/events/{TOPIC_DECISION_MADE}",
                json=cloudevent(_decision(route="auto_approve", usd=4200, ceil=25000)))
    assert await trail.auto_approval_count() == 1
    assert await trail.ceiling_violations() == []
    # non-auto route does NOT touch the index
    client.post(f"/events/{TOPIC_DECISION_MADE}",
                json=cloudevent(_decision(route="human_review", eid="evt-hr")))
    assert await trail.auto_approval_count() == 1


async def test_redelivered_event_appends_once():
    client, trail = make_env()
    ev = cloudevent(_submitted())
    client.post(f"/events/{TOPIC_INVOICE_SUBMITTED}", json=ev)
    resp = client.post(f"/events/{TOPIC_INVOICE_SUBMITTED}", json=ev)  # same event_id
    assert resp.status_code == 200
    assert len(await trail.get_trail("corr-1")) == 1


def test_payment_failed_is_recorded():
    client, trail = make_env()
    failed = PaymentFailedPayload(
        meta=new_event_meta("inv-1", "corr-1").model_copy(update={"event_id": "evt-f"}),
        reason="insufficient_budget", compensated=False,
    ).model_dump()
    resp = client.post(f"/events/{TOPIC_PAYMENT_FAILED}", json=cloudevent(failed))
    assert resp.status_code == 200


async def test_post_append_failure_500s_forgets_and_redelivery_records_once():
    """Plan Step-1 required test + the guard for the partial-failure double-append
    bug: the auto-approval handler does TWO writes (trail entry, then the global
    ceiling index). Fail the SECOND once; the handler 500s and forgets the dedupe
    mark, so Dapr redelivers -- and because append/append_auto_approval are
    idempotent by content, the redelivery records EXACTLY ONE trail entry and ONE
    index row, never two."""
    import pytest

    from afcommon.dedupe import EventDedupe
    from afcommon.state import InMemoryStateStore

    real_trail = AuditTrail(InMemoryStateStore())
    dedupe = EventDedupe(InMemoryStateStore())

    calls = {"n": 0}
    real_index = real_trail.append_auto_approval

    async def flaky_index(entry: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("index store hiccup")
        await real_index(entry)

    real_trail.append_auto_approval = flaky_index  # type: ignore[method-assign]

    app.dependency_overrides[deps.get_trail] = lambda: real_trail
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    client = TestClient(app, raise_server_exceptions=False)

    ev = cloudevent(_decision(route="auto_approve", eid="evt-aa", usd=4200, ceil=25000))
    first = client.post(f"/events/{TOPIC_DECISION_MADE}", json=ev)
    assert first.status_code == 500  # index write failed → 500 for redelivery

    second = client.post(f"/events/{TOPIC_DECISION_MADE}", json=ev)  # Dapr redelivery
    assert second.status_code == 200

    # Exactly one trail entry and one index row despite the replayed first write.
    entries = await real_trail.get_trail("corr-1")
    assert len(entries) == 1
    assert await real_trail.auto_approval_count() == 1
