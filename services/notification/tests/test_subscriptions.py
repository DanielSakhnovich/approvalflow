"""Task 4: notification-svc subscribes to outcome events and turns each into a
submitter-facing notification, delivered through the vendored HW3 processor.
Only human_review / reject decisions and terminal payment events notify;
auto_approve decisions do not (the terminal payment event covers the outcome).
"""

from afcommon.contracts import (
    DecisionMadePayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
from afcommon.events import (
    TOPIC_DECISION_MADE,
    TOPIC_PAYMENT_COMPLETED,
    TOPIC_PAYMENT_FAILED,
    new_event_meta,
)
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.notification.src import deps, storage
from services.notification.src.main import app
from services.notification.src.models import SENT


class SpyProcessor:
    """Deterministic stand-in for the random-provider processor: records the
    notifications it was asked to send and marks them SENT."""
    def __init__(self):
        self.sent = []

    async def send_one(self, n):
        n.status = SENT
        self.sent.append(n)


def make_env():
    storage.clear()
    from afcommon.dedupe import EventDedupe
    spy = SpyProcessor()
    dedupe = EventDedupe(InMemoryStateStore())
    app.dependency_overrides[deps.get_processor] = lambda: spy
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    return TestClient(app), spy


def teardown_function(_fn):
    app.dependency_overrides.clear()
    storage.clear()


def cloudevent(payload: dict) -> dict:
    return {"data": payload}


def _decision(route, eid="evt-d"):
    return DecisionMadePayload(
        meta=new_event_meta("inv-1", "corr-1").model_copy(update={"event_id": eid}),
        route=route, recommendation="approve", confidence=0.9, violations=[],
        reasoning="Client dinner missing client name.", usd_cents=100, ceiling_cents=25000,
    ).model_dump()


def _paid(eid="evt-p"):
    return PaymentCompletedPayload(
        meta=new_event_meta("inv-1", "corr-1").model_copy(update={"event_id": eid}),
        amount_cents=100, budget_remaining_cents=999900, department="engineering-2026Q2",
    ).model_dump()


def _failed(eid="evt-f"):
    return PaymentFailedPayload(
        meta=new_event_meta("inv-1", "corr-1").model_copy(update={"event_id": eid}),
        reason="insufficient_budget", compensated=False,
    ).model_dump()


def test_subscribe_lists_the_three_topics():
    client, _ = make_env()
    subs = client.get("/dapr/subscribe").json()
    assert {s["topic"] for s in subs} == {
        TOPIC_DECISION_MADE, TOPIC_PAYMENT_COMPLETED, TOPIC_PAYMENT_FAILED}
    assert all(s["pubsubname"] == "pubsub" for s in subs)


def test_payment_completed_creates_and_sends_notification():
    client, spy = make_env()
    resp = client.post(f"/events/{TOPIC_PAYMENT_COMPLETED}", json=cloudevent(_paid()))
    assert resp.status_code == 200
    assert len(spy.sent) == 1
    assert spy.sent[0].status == SENT
    assert len(storage.get_all()) == 1


def test_payment_failed_notifies():
    client, spy = make_env()
    client.post(f"/events/{TOPIC_PAYMENT_FAILED}", json=cloudevent(_failed()))
    assert len(spy.sent) == 1


def test_decision_human_review_notifies():
    client, spy = make_env()
    client.post(f"/events/{TOPIC_DECISION_MADE}",
                json=cloudevent(_decision("human_review")))
    assert len(spy.sent) == 1


def test_decision_auto_approve_does_not_notify():
    client, spy = make_env()
    resp = client.post(f"/events/{TOPIC_DECISION_MADE}",
                       json=cloudevent(_decision("auto_approve")))
    assert resp.status_code == 200  # acked
    assert spy.sent == []           # but no notification (terminal payment covers it)
    assert storage.get_all() == []


def test_redelivered_event_notifies_once():
    client, spy = make_env()
    ev = cloudevent(_paid())
    client.post(f"/events/{TOPIC_PAYMENT_COMPLETED}", json=ev)
    resp = client.post(f"/events/{TOPIC_PAYMENT_COMPLETED}", json=ev)  # same event_id
    assert resp.status_code == 200
    assert len(spy.sent) == 1  # dedupe: one notification only


def test_notifications_endpoint_lists_created():
    client, _ = make_env()
    client.post(f"/events/{TOPIC_PAYMENT_COMPLETED}", json=cloudevent(_paid()))
    resp = client.get("/notifications")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
