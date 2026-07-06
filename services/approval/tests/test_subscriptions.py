"""
Task 2: the decision-made subscriber that turns a `human_review` route into
a durable escalation record -- the "pause" of the human-in-the-loop (D-013).

Key ordering fact under test: the route filter runs BEFORE the dedupe mark.
A non-`human_review` route is acked without ever touching
`processed:{event_id}`, so it must not consume dedupe budget for that
event_id -- proven (contrived but deliberately so) by replaying the *same*
event_id with route=`human_review` afterwards and confirming it still
processes as if it were the first delivery.
"""

import pytest
from afcommon.contracts import DecisionMadePayload
from afcommon.dedupe import EventDedupe
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.approval.src import deps
from services.approval.src.main import app
from services.approval.src.models import EscalationStatus
from services.approval.src.repo import ApprovalRepo


class _FailOnceThenSucceedRepo(ApprovalRepo):
    """Raises on its first `save_new` call -- simulating the state store
    blowing up after the dedupe mark is already persisted -- then behaves
    normally on every call after. Mirrors decision-svc's
    `_FailOnceThenSucceedPublisher` (test_pipeline.py)."""

    def __init__(self, store):
        super().__init__(store)
        self._raised_once = False

    async def save_new(self, esc):
        if not self._raised_once:
            self._raised_once = True
            raise RuntimeError("simulated repo failure")
        return await super().save_new(esc)


def cloudevent(payload: dict) -> dict:
    return {"specversion": "1.0", "type": "com.dapr.event.sent", "topic": "x", "data": payload}


def decision_payload(
    invoice_id: str,
    route: str = "human_review",
    event_id: str | None = None,
    correlation_id: str = "corr-1",
) -> dict:
    meta = new_event_meta(invoice_id, correlation_id)
    if event_id is not None:
        meta = meta.model_copy(update={"event_id": event_id})
    return DecisionMadePayload(
        meta=meta,
        route=route,
        recommendation="Escalate to management",
        confidence=0.5,
        violations=["policy_a"],
        reasoning="High value and policy violation",
        usd_cents=50000,
        ceiling_cents=25000,
    ).model_dump()


@pytest.fixture
def env():
    store = InMemoryStateStore()
    repo = ApprovalRepo(store)
    dedupe = EventDedupe(store)
    app.dependency_overrides[deps.get_repo] = lambda: repo
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    client = TestClient(app, raise_server_exceptions=False)
    yield client, repo, dedupe
    app.dependency_overrides.clear()


def test_subscribe_lists_decision_made_topic(env):
    client, _repo, _dedupe = env
    subs = client.get("/dapr/subscribe").json()
    assert subs == [
        {"pubsubname": "pubsub", "topic": "decision-made", "route": "/events/decision-made"}
    ]


async def test_human_review_event_records_pending_and_queues(env):
    client, repo, _dedupe = env
    payload = decision_payload("inv-1")
    resp = client.post("/events/decision-made", json=cloudevent(payload))
    assert resp.status_code == 200

    stored = await repo.get("inv-1")
    assert stored is not None
    assert stored.status == EscalationStatus.pending
    assert stored.usd_cents == 50000

    queue = await repo.list_queue()
    assert queue == ["inv-1"]


class TestNonMatchingRoutesSkipDedupe:
    """auto_approve/reject/duplicate must ack without storing anything AND
    without consuming dedupe budget for that event_id."""

    async def test_non_matching_routes_are_acked_with_nothing_stored(self, env):
        client, repo, _dedupe = env
        for route in ("auto_approve", "reject", "duplicate"):
            payload = decision_payload(f"inv-{route}", route=route)
            resp = client.post("/events/decision-made", json=cloudevent(payload))
            assert resp.status_code == 200
            stored = await repo.get(f"inv-{route}")
            assert stored is None
        queue = await repo.list_queue()
        assert queue == []

    async def test_non_matching_route_does_not_consume_dedupe_budget(self, env):
        """Contrived-but-proving: replay the SAME event_id, first as a
        non-matching route, then as human_review. If the filter ran before
        the dedupe mark (correct), the second delivery still processes as
        first-time. If dedupe were marked first (wrong), the second
        delivery would be silently skipped as a duplicate and no escalation
        would ever be created."""
        client, repo, _dedupe = env
        shared_event_id = "evt-shared-1"
        skip_payload = decision_payload("inv-2", route="auto_approve", event_id=shared_event_id)
        resp1 = client.post("/events/decision-made", json=cloudevent(skip_payload))
        assert resp1.status_code == 200

        review_payload = decision_payload(
            "inv-2", route="human_review", event_id=shared_event_id
        )
        resp2 = client.post("/events/decision-made", json=cloudevent(review_payload))
        assert resp2.status_code == 200

        stored = await repo.get("inv-2")
        assert stored is not None
        assert stored.status == EscalationStatus.pending


async def test_redelivered_human_review_event_applies_once(env):
    client, repo, _dedupe = env
    payload = decision_payload("inv-3")  # same event_id both times
    r1 = client.post("/events/decision-made", json=cloudevent(payload))
    r2 = client.post("/events/decision-made", json=cloudevent(payload))
    assert r1.status_code == 200
    assert r2.status_code == 200

    queue = await repo.list_queue()
    assert queue == ["inv-3"]


async def test_already_escalated_invoice_acks_without_touching_queue(env):
    """A second, distinct decision-made event (different event_id) for an
    invoice that already has an escalation record -- e.g. decision-svc
    re-publishing -- must be acked, log, and leave the queue untouched
    rather than calling add_to_queue again."""
    client, repo, _dedupe = env
    first = decision_payload("inv-4", event_id="evt-a")
    client.post("/events/decision-made", json=cloudevent(first))
    queue_after_first = await repo.list_queue()
    assert queue_after_first == ["inv-4"]

    second = decision_payload("inv-4", event_id="evt-b")
    resp = client.post("/events/decision-made", json=cloudevent(second))
    assert resp.status_code == 200

    queue_after_second = await repo.list_queue()
    assert queue_after_second == ["inv-4"]


def test_post_mark_failure_forgets_dedupe_so_redelivery_succeeds():
    """THE KEY TEST: if the handler body raises after the dedupe mark is
    already persisted (e.g. the repo blows up on save_new), the first
    delivery must 500 (so Dapr redelivers) AND the mark must be forgotten
    (so the redelivered copy of the SAME event_id is reprocessed for real
    instead of being silently swallowed as a stale duplicate)."""
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    flaky_repo = _FailOnceThenSucceedRepo(store)
    app.dependency_overrides[deps.get_repo] = lambda: flaky_repo
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    client = TestClient(app, raise_server_exceptions=False)
    try:
        payload = decision_payload("inv-5")  # same event_id both times

        first = client.post("/events/decision-made", json=cloudevent(payload))
        assert first.status_code == 500

        second = client.post("/events/decision-made", json=cloudevent(payload))
        assert second.status_code == 200
    finally:
        app.dependency_overrides.clear()
