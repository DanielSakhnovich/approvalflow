import pytest
from afcommon.contracts import (
    ApprovalResolvedPayload,
    DecisionMadePayload,
    PaymentCompletedPayload,
    PaymentFailedPayload,
)
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


def test_approval_resolved_event_applies_verdict_and_counters(env):
    client, repo = env
    tracking = submit(client)
    # Send decision with human_review route to set status to pending_approval
    client.post("/events/decision-made",
                json=cloudevent(decision_payload(tracking, route="human_review")))
    # Send approval-resolved event with verdict=approved
    approval = ApprovalResolvedPayload(
        meta=new_event_meta(tracking, "corr_1"), verdict="approved",
        approver_id="lena@northwind.example", comment="ok").model_dump()
    resp = client.post("/events/approval-resolved", json=cloudevent(approval))
    assert resp.status_code == 200
    # Verify record status and decided_by
    record_resp = client.get(f"/api/invoices/{tracking}").json()
    assert record_resp["status"] == "approved"
    assert record_resp["decidedBy"] == "lena@northwind.example"
    # Verify dashboard counter
    counters = client.get("/api/dashboard").json()
    assert counters["verdict_approved"] == 1


def test_payment_failed_event_and_human_paid_counter(env):
    client, repo = env
    # Test payment_completed with human approval
    tracking1 = submit(client)
    client.post("/events/decision-made",
                json=cloudevent(decision_payload(tracking1, route="human_review")))
    approval = ApprovalResolvedPayload(
        meta=new_event_meta(tracking1, "corr_1"), verdict="approved",
        approver_id="lena@northwind.example", comment="ok").model_dump()
    client.post("/events/approval-resolved", json=cloudevent(approval))
    # Now complete the payment for human-approved invoice
    pay = PaymentCompletedPayload(
        meta=new_event_meta(tracking1, "corr_1"), amount_cents=4200,
        budget_remaining_cents=1, department="engineering-2026Q2").model_dump()
    resp = client.post("/events/payment-completed", json=cloudevent(pay))
    assert resp.status_code == 200
    counters = client.get("/api/dashboard").json()
    assert counters["paid"] == 1
    assert counters["paid_human_cents"] == 4200

    # Test payment_failed event on an approved invoice
    tracking2 = submit(client)
    client.post("/events/decision-made",
                json=cloudevent(decision_payload(tracking2)))  # auto_approve
    # Now try to fail payment on an approved invoice
    failed = PaymentFailedPayload(
        meta=new_event_meta(tracking2, "corr_2"), reason="insufficient funds",
        compensated=False).model_dump()
    resp = client.post("/events/payment-failed", json=cloudevent(failed))
    assert resp.status_code == 200
    record_resp = client.get(f"/api/invoices/{tracking2}").json()
    assert record_resp["status"] == "payment_failed"
    assert "insufficient funds" in record_resp["reasoning"]
    counters = client.get("/api/dashboard").json()
    assert counters["payment_failed"] == 1
