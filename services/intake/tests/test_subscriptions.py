import pytest
from afcommon.contracts import DecisionMadePayload, PaymentCompletedPayload
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.intake.src import deps
from services.intake.src.main import app
from services.intake.src.repo import IntakeRepo
from services.intake.tests.test_models import FIXTURE


@pytest.fixture
def env():
    repo = IntakeRepo(InMemoryStateStore())

    async def fake_publish(topic, payload):
        pass

    app.dependency_overrides[deps.get_repo] = lambda: repo
    app.dependency_overrides[deps.get_publisher] = lambda: fake_publish
    yield TestClient(app), repo
    app.dependency_overrides.clear()


def cloudevent(payload: dict) -> dict:
    return {"specversion": "1.0", "type": "com.dapr.event.sent",
            "topic": "x", "data": payload}


def submit(client) -> str:
    return client.post("/api/invoices", json=FIXTURE).json()["trackingId"]


def decision_payload(invoice_id: str, route: str = "auto_approve") -> dict:
    return DecisionMadePayload(
        meta=new_event_meta(invoice_id, "corr_1"), route=route, recommendation="approve",
        confidence=0.95, violations=[], reasoning="In policy.", usd_cents=4200,
        ceiling_cents=25000,
    ).model_dump()


def test_subscribe_lists_all_five_topics(env):
    client, _ = env
    subs = client.get("/dapr/subscribe").json()
    assert {s["topic"] for s in subs} == {
        "invoice-submitted", "decision-made", "approval-resolved",
        "payment-completed", "payment-failed"}
    assert all(s["pubsubname"] == "pubsub" for s in subs)


def test_decision_event_updates_status(env):
    client, _ = env
    tracking = submit(client)
    resp = client.post("/events/decision-made", json=cloudevent(decision_payload(tracking)))
    assert resp.status_code == 200
    assert client.get(f"/api/invoices/{tracking}").json()["status"] == "approved"


def test_redelivered_event_applies_once(env):
    client, _ = env
    tracking = submit(client)
    payload = decision_payload(tracking)          # same event_id both times
    client.post("/events/decision-made", json=cloudevent(payload))
    resp = client.post("/events/decision-made", json=cloudevent(payload))
    assert resp.status_code == 200                # acked, not retried
    assert client.get(f"/api/invoices/{tracking}").json()["status"] == "approved"


def test_out_of_order_event_is_acked_not_crashed(env):
    client, _ = env
    tracking = submit(client)
    pay = PaymentCompletedPayload(
        meta=new_event_meta(tracking, "corr_1"), amount_cents=4200,
        budget_remaining_cents=1, department="engineering-2026Q2").model_dump()
    resp = client.post("/events/payment-completed", json=cloudevent(pay))
    assert resp.status_code == 200                # evaluating + payment event = illegal; ack + log
    assert client.get(f"/api/invoices/{tracking}").json()["status"] == "evaluating"


def test_paid_flow_bumps_money_counters(env):
    client, _ = env
    tracking = submit(client)
    client.post("/events/decision-made", json=cloudevent(decision_payload(tracking)))
    pay = PaymentCompletedPayload(
        meta=new_event_meta(tracking, "corr_1"), amount_cents=4200,
        budget_remaining_cents=1, department="engineering-2026Q2").model_dump()
    client.post("/events/payment-completed", json=cloudevent(pay))
    counters = client.get("/api/dashboard").json()
    assert counters["decided_auto_approve"] == 1
    assert counters["paid"] == 1 and counters["paid_auto_cents"] == 4200


def test_unknown_invoice_event_is_acked(env):
    client, _ = env
    resp = client.post("/events/decision-made",
                       json=cloudevent(decision_payload("inv_ghost")))
    assert resp.status_code == 200
