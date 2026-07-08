"""Task 4: Dapr subscriptions for the payment service.

Key ordering fact under test (mirrors approval-svc's T2 subscriber): each
handler's route/verdict filter runs BEFORE the dedupe mark, so a
filtered-out delivery must not consume dedupe budget for that event_id --
proven the same contrived-but-deliberate way, by replaying the SAME
event_id with a matching route/verdict afterwards and confirming it still
processes as first-time.

The graded double (two DISTINCT approval-resolved events, fresh event_ids,
same invoice) proves per-invoice idempotency lives in PaymentSaga itself,
not in dedupe: dedupe only guards per-event_id redelivery, so a second,
genuinely new event_id sails past it -- and it's the saga's own terminal
`saga:{invoice_id}` record that stops a second payment from happening.
"""

import pytest
from afcommon.contracts import ApprovalResolvedPayload, DecisionMadePayload
from afcommon.dedupe import EventDedupe
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.payment.src import deps
from services.payment.src.budgets import BudgetStore
from services.payment.src.main import app
from services.payment.src.provider import MockPaymentProvider
from services.payment.src.saga import PaymentSaga, SagaState

# Seeded to 5,000,000 cents ($50,000) by sample-invoices.json.
_DEPT = "engineering-2026Q2"


class _CapturingPublisher:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, topic: str, payload: dict) -> None:
        self.events.append((topic, payload))


class _CountingProvider(MockPaymentProvider):
    def __init__(self, store, injection_enabled):
        super().__init__(store, injection_enabled)
        self.execute_calls = 0

    async def execute(self, invoice_id, amount_cents, scenario):
        self.execute_calls += 1
        return await super().execute(invoice_id, amount_cents, scenario)


class _SpySaga:
    """Records every `handle()` call's kwargs without doing any real work --
    for proving a filtered-out route/verdict NEVER reaches the saga at all."""

    def __init__(self):
        self.calls: list[dict] = []

    async def handle(self, **kwargs):
        self.calls.append(kwargs)
        return None


class _FailOnceThenSucceedSaga(PaymentSaga):
    """Raises on its first `handle()` call -- simulating saga infra (state
    store, provider) blowing up after the dedupe mark is already persisted
    -- then behaves normally on every call after. Mirrors decision-svc's
    `_FailOnceThenSucceedPublisher` / approval's `_FailOnceThenSucceedRepo`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raised_once = False

    async def handle(self, *args, **kwargs):
        if not self._raised_once:
            self._raised_once = True
            raise RuntimeError("simulated saga infra failure")
        return await super().handle(*args, **kwargs)


def cloudevent(payload: dict) -> dict:
    return {"specversion": "1.0", "type": "com.dapr.event.sent", "topic": "x", "data": payload}


def decision_payload(
    invoice_id: str,
    route: str = "auto_approve",
    event_id: str | None = None,
    correlation_id: str = "corr-1",
    usd_cents: int = 40000,
    ceiling_cents: int = 100000,
    scenario: str = "",
    department: str = _DEPT,
) -> dict:
    meta = new_event_meta(invoice_id, correlation_id)
    if event_id is not None:
        meta = meta.model_copy(update={"event_id": event_id})
    return DecisionMadePayload(
        meta=meta,
        route=route,
        recommendation="Auto-approved: under ceiling",
        confidence=0.95,
        violations=[],
        reasoning="Under $250 ceiling, known vendor, receipt present",
        usd_cents=usd_cents,
        ceiling_cents=ceiling_cents,
        scenario=scenario,
        department=department,
    ).model_dump()


def approval_payload(
    invoice_id: str,
    verdict: str = "approved",
    event_id: str | None = None,
    correlation_id: str = "corr-1",
    usd_cents: int = 40000,
    scenario: str = "",
    department: str = _DEPT,
) -> dict:
    meta = new_event_meta(invoice_id, correlation_id)
    if event_id is not None:
        meta = meta.model_copy(update={"event_id": event_id})
    return ApprovalResolvedPayload(
        meta=meta,
        verdict=verdict,
        approver_id="lena@northwind.example",
        comment="",
        usd_cents=usd_cents,
        scenario=scenario,
        department=department,
    ).model_dump()


@pytest.fixture
def env():
    """Real saga wired to real BudgetStore/MockPaymentProvider (injection
    disabled) over a shared InMemoryStateStore, plus a real EventDedupe over
    the same store -- an end-to-end path from HTTP in to saga effect out."""
    store = InMemoryStateStore()
    budgets = BudgetStore(store)
    provider = _CountingProvider(store, injection_enabled=False)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, store, publisher)
    dedupe = EventDedupe(store)
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_saga] = lambda: saga
    app.dependency_overrides[deps.get_budget_store] = lambda: budgets
    client = TestClient(app, raise_server_exceptions=False)
    yield client, budgets, provider, saga, publisher, dedupe, store
    app.dependency_overrides.clear()


@pytest.fixture
def spy_env():
    """A spy saga standing in for PaymentSaga -- for proving filtered-out
    deliveries never reach the saga at all."""
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    saga = _SpySaga()
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_saga] = lambda: saga
    client = TestClient(app, raise_server_exceptions=False)
    yield client, saga, dedupe
    app.dependency_overrides.clear()


def test_subscribe_lists_both_topics(env):
    client, *_ = env
    subs = client.get("/dapr/subscribe").json()
    assert subs == [
        {"pubsubname": "pubsub", "topic": "decision-made", "route": "/events/decision-made"},
        {
            "pubsubname": "pubsub",
            "topic": "approval-resolved",
            "route": "/events/approval-resolved",
        },
    ]


async def test_auto_approve_decision_runs_saga_pays_and_publishes(env):
    client, budgets, provider, saga, publisher, _dedupe, store = env
    before = await budgets.get_remaining(_DEPT)

    payload = decision_payload("inv-1", route="auto_approve", usd_cents=40000)
    resp = client.post("/events/decision-made", json=cloudevent(payload))
    assert resp.status_code == 200

    value, _etag = await store.get("saga:inv-1")
    assert value is not None
    assert value["state"] == SagaState.paid
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000

    topics = [t for t, _p in publisher.events]
    assert topics == ["payment-completed"]


async def test_human_review_decision_acked_pre_dedupe_saga_never_invoked(spy_env):
    client, saga, _dedupe = spy_env
    payload = decision_payload("inv-2", route="human_review")
    resp = client.post("/events/decision-made", json=cloudevent(payload))
    assert resp.status_code == 200
    assert saga.calls == []


async def test_non_auto_approve_route_does_not_consume_dedupe_budget(spy_env):
    """Contrived-but-proving: replay the SAME event_id, first as a
    non-matching route, then as auto_approve. If the filter ran before the
    dedupe mark (correct), the second delivery still reaches the saga."""
    client, saga, _dedupe = spy_env
    shared_event_id = "evt-shared-1"
    skip_payload = decision_payload("inv-3", route="human_review", event_id=shared_event_id)
    resp1 = client.post("/events/decision-made", json=cloudevent(skip_payload))
    assert resp1.status_code == 200
    assert saga.calls == []

    auto_payload = decision_payload("inv-3", route="auto_approve", event_id=shared_event_id)
    resp2 = client.post("/events/decision-made", json=cloudevent(auto_payload))
    assert resp2.status_code == 200
    assert len(saga.calls) == 1


async def test_approval_resolved_approved_runs_saga_with_auto_route_false(spy_env):
    client, saga, _dedupe = spy_env
    payload = approval_payload("inv-4", verdict="approved", usd_cents=200000)
    resp = client.post("/events/approval-resolved", json=cloudevent(payload))
    assert resp.status_code == 200

    assert len(saga.calls) == 1
    call = saga.calls[0]
    assert call["invoice_id"] == "inv-4"
    assert call["department"] == _DEPT
    assert call["amount_cents"] == 200000
    assert call["auto_route"] is False
    assert call["ceiling_cents"] is None


async def test_approval_resolved_rejected_acked_pre_dedupe_no_saga(spy_env):
    client, saga, _dedupe = spy_env
    payload = approval_payload("inv-5", verdict="rejected")
    resp = client.post("/events/approval-resolved", json=cloudevent(payload))
    assert resp.status_code == 200
    assert saga.calls == []


async def test_approval_resolved_needs_info_acked_pre_dedupe_no_saga(spy_env):
    client, saga, _dedupe = spy_env
    payload = approval_payload("inv-5b", verdict="needs_info")
    resp = client.post("/events/approval-resolved", json=cloudevent(payload))
    assert resp.status_code == 200
    assert saga.calls == []


async def test_redelivered_decision_made_event_has_one_saga_effect(env):
    client, budgets, provider, _saga, publisher, _dedupe, _store = env
    before = await budgets.get_remaining(_DEPT)
    payload = decision_payload("inv-6", usd_cents=40000)  # same event_id both times

    r1 = client.post("/events/decision-made", json=cloudevent(payload))
    r2 = client.post("/events/decision-made", json=cloudevent(payload))
    assert r1.status_code == 200
    assert r2.status_code == 200

    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000
    topics = [t for t, _p in publisher.events]
    assert topics == ["payment-completed"]


def test_post_mark_saga_failure_returns_500_and_forgets_dedupe():
    """THE KEY TEST: if saga.handle() raises after the dedupe mark is
    already persisted, the first delivery must 500 (so Dapr redelivers) AND
    the mark must be forgotten (so the redelivered copy of the SAME
    event_id is reprocessed for real, not silently swallowed)."""
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    budgets = BudgetStore(store)
    provider = _CountingProvider(store, injection_enabled=False)
    publisher = _CapturingPublisher()
    flaky_saga = _FailOnceThenSucceedSaga(budgets, provider, store, publisher)
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_saga] = lambda: flaky_saga
    client = TestClient(app, raise_server_exceptions=False)
    try:
        payload = decision_payload("inv-7", usd_cents=40000)  # same event_id both times

        first = client.post("/events/decision-made", json=cloudevent(payload))
        assert first.status_code == 500

        second = client.post("/events/decision-made", json=cloudevent(payload))
        assert second.status_code == 200
        assert provider.execute_calls == 1
    finally:
        app.dependency_overrides.clear()


async def test_two_fresh_events_same_invoice_yield_exactly_one_payment(env):
    """THE GRADED DOUBLE: two DISTINCT approval-resolved events (fresh
    event_ids, so dedupe never blocks either one) for the SAME invoice must
    still produce exactly one payment -- the second is acked via the
    saga's own terminal `saga:{invoice_id}` record, not via dedupe."""
    client, budgets, provider, _saga, publisher, _dedupe, store = env
    before = await budgets.get_remaining(_DEPT)

    first = approval_payload("inv-8", verdict="approved", usd_cents=50000, event_id="evt-8a")
    second = approval_payload("inv-8", verdict="approved", usd_cents=50000, event_id="evt-8b")

    r1 = client.post("/events/approval-resolved", json=cloudevent(first))
    r2 = client.post("/events/approval-resolved", json=cloudevent(second))
    assert r1.status_code == 200
    assert r2.status_code == 200

    value, _etag = await store.get("saga:inv-8")
    assert value["state"] == SagaState.paid
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 50000

    payment_completed_events = [p for t, p in publisher.events if t == "payment-completed"]
    assert len(payment_completed_events) == 1


async def test_get_budget_known_department_returns_remaining(env):
    client, budgets, *_ = env
    await budgets.seed_if_absent()
    resp = client.get(f"/api/budgets/{_DEPT}")
    assert resp.status_code == 200
    assert resp.json() == {"department": _DEPT, "remaining_cents": 5000000}


async def test_get_budget_unknown_department_returns_404(env):
    client, budgets, *_ = env
    await budgets.seed_if_absent()
    resp = client.get("/api/budgets/nonexistent-dept")
    assert resp.status_code == 404
