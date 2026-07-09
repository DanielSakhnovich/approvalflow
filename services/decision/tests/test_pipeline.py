"""
Task 7 — the wired decision pipeline: gates in order, trust history, and the
Dapr subscriptions that drive it.

Section 1 exercises `DecisionPipeline` directly with fakes (InMemoryStateStore,
StubAgent, a capturing publisher) — no FastAPI involved, per the brief.
Section 2 exercises the actual `/events/*` HTTP handlers via TestClient with
dependency overrides, proving the wiring (dedupe, context binding, ack
semantics, infra-failure 500) end-to-end.
"""

import json
from pathlib import Path

import pytest
from afcommon.contracts import InvoiceSubmittedPayload, PaymentCompletedPayload
from afcommon.dedupe import EventDedupe
from afcommon.events import TOPIC_DECISION_MADE, new_event_meta
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.decision.src import deps
from services.decision.src.agents.handrolled import ProviderUnavailable
from services.decision.src.agents.stub import StubAgent
from services.decision.src.config import ConfigRepo
from services.decision.src.fingerprint import FingerprintRegistry
from services.decision.src.main import app
from services.decision.src.pipeline import DecisionPipeline
from services.decision.src.retrieval import PolicyRetriever, chunk_policy
from services.decision.src.trust import TrustRepo


def _fixture(fixture_id: str) -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "sample-invoices.json").exists():
            data = json.loads((parent / "sample-invoices.json").read_text())
            return next(f for f in data["fixtures"] if f["id"] == fixture_id)
    raise FileNotFoundError("sample-invoices.json not found")


class _CapturingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, topic: str, payload: dict) -> None:
        self.calls.append((topic, payload))


class _CountingAgent:
    """Wraps a real agent, counting every `evaluate()` call — used to prove
    the pipeline never even invokes the agent for a duplicate."""

    def __init__(self, agent) -> None:
        self._agent = agent
        self.calls = 0

    async def evaluate(self, invoice: dict, policy_rules: str):
        self.calls += 1
        return await self._agent.evaluate(invoice, policy_rules)


class _FlakyAgent:
    """Always raises ProviderUnavailable — simulates an exhausted-retries
    provider (M15) for the AGENT-UNAVAILABLE path."""

    async def evaluate(self, invoice: dict, policy_rules: str):
        raise ProviderUnavailable("simulated provider outage")


class _RecordingAgent:
    """Wraps a real agent, recording the `policy_rules` string it was called
    with — used to prove RAG narrows the prompt actually handed to the agent
    (full text when RAG is off/no retriever, the retriever's rendered subset
    when RAG is on)."""

    def __init__(self, agent) -> None:
        self._agent = agent
        self.policy_rules_seen: list[str] = []

    async def evaluate(self, invoice: dict, policy_rules: str):
        self.policy_rules_seen.append(policy_rules)
        return await self._agent.evaluate(invoice, policy_rules)


_POLICY_TEXT = "(policy text not exercised by StubAgent)"


def _policy_retriever() -> PolicyRetriever:
    """Builds a real `PolicyRetriever` over the repo's actual `policy.md` —
    the same source deps.py wires in production — so RAG tests exercise
    genuine chunking/retrieval, not a synthetic stand-in."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "policy.md").exists():
            policy_md = (parent / "policy.md").read_text()
            return PolicyRetriever(chunk_policy(policy_md))
    raise FileNotFoundError("policy.md not found")


def _make_pipeline(
    agent,
    publisher: _CapturingPublisher | None = None,
    store: InMemoryStateStore | None = None,
    retriever: PolicyRetriever | None = None,
) -> tuple[DecisionPipeline, TrustRepo, _CapturingPublisher, InMemoryStateStore]:
    store = store or InMemoryStateStore()
    config = ConfigRepo(store)
    fingerprints = FingerprintRegistry(store)
    trust = TrustRepo(store)
    publisher = publisher or _CapturingPublisher()
    pipeline = DecisionPipeline(
        config=config,
        fingerprints=fingerprints,
        trust=trust,
        agent=agent,
        publisher=publisher,
        policy_rules=_POLICY_TEXT,
        retriever=retriever,
    )
    return pipeline, trust, publisher, store


def _submission(invoice: dict, correlation_id: str = "corr-1") -> InvoiceSubmittedPayload:
    return InvoiceSubmittedPayload(
        meta=new_event_meta(invoice["id"], correlation_id), invoice=invoice
    )


# --- Section 1: DecisionPipeline, fakes only --------------------------------


async def test_inv1001_auto_approves_and_publishes_decision_made_once():
    pipeline, _trust, publisher, _store = _make_pipeline(StubAgent())
    invoice = _fixture("INV-1001")

    result = await pipeline.handle_submission(_submission(invoice))

    assert result.route == "auto_approve"
    assert result.usd_cents == 4200
    assert len(publisher.calls) == 1
    topic, published = publisher.calls[0]
    assert topic == TOPIC_DECISION_MADE
    assert published["route"] == "auto_approve"
    assert published["usd_cents"] == 4200
    # Fresh event id, but same invoice/correlation ids as the submission.
    assert published["meta"]["invoice_id"] == invoice["id"]
    assert published["meta"]["correlation_id"] == "corr-1"
    # INV-1001 has no `scenario` in the fixture and a real department: the
    # marker stays empty (production-shaped invoice) while department rides
    # through for payment (Phase 05) to size its reservation against.
    assert published["scenario"] == ""
    assert published["department"] == "engineering-2026Q2"


async def test_inv1012_carries_scenario_and_department_through_to_decision_made():
    """INV-1012 is the fixture with a `scenario` marker
    (payment-failure:journey-D), used by Phase 05's provider gate for
    fixture-driven failure injection. Both scenario and department must
    ride the published DecisionMadePayload unchanged from the invoice."""
    pipeline, _trust, publisher, _store = _make_pipeline(StubAgent())
    invoice = _fixture("INV-1012")

    result = await pipeline.handle_submission(_submission(invoice))

    assert result.route == "human_review"
    assert result.scenario == "payment-failure:journey-D"
    assert result.department == "engineering-2026Q2"
    _topic, published = publisher.calls[0]
    assert published["scenario"] == "payment-failure:journey-D"
    assert published["department"] == "engineering-2026Q2"


async def test_duplicate_is_routed_without_ever_calling_the_agent():
    """INV-1007 is a byte-for-byte resubmission of INV-1001. Once INV-1001 is
    processed, INV-1007 must come back `duplicate` -- and, crucially, the
    agent must receive ZERO additional calls for it (D-011: duplicates never
    reach the agent, not merely 'reach it and get ignored')."""
    store = InMemoryStateStore()
    counting_agent = _CountingAgent(StubAgent())
    pipeline, _trust, publisher, _store = _make_pipeline(counting_agent, store=store)

    inv1001, inv1007 = _fixture("INV-1001"), _fixture("INV-1007")
    first = await pipeline.handle_submission(_submission(inv1001))
    assert first.route == "auto_approve"
    calls_after_first = counting_agent.calls
    assert calls_after_first == 1

    second = await pipeline.handle_submission(_submission(inv1007, correlation_id="corr-2"))

    assert second.route == "duplicate"
    assert second.violations == ["GLOBAL-DUP"]
    assert counting_agent.calls == calls_after_first, "agent must not be called for a duplicate"
    assert len(publisher.calls) == 2  # both invoices still each get one decision-made


async def test_rag_on_with_retriever_adds_retrieved_rules_without_changing_route(monkeypatch):
    """A meals invoice run through a pipeline WITH a retriever + RAG on must
    (a) publish non-empty retrieved_rules containing a MEAL-* id, (b) narrow
    the policy text the agent actually receives, and (c) reach the SAME
    route as the identical invoice run WITHOUT a retriever at all -- RAG
    must never move the router's decision, only the prompt/evidence."""
    monkeypatch.setenv("RAG_ENABLED", "true")
    invoice = _fixture("INV-1001")  # meals, auto_approve

    no_rag_agent = _RecordingAgent(StubAgent())
    no_rag_pipeline, *_ = _make_pipeline(no_rag_agent)
    baseline = await no_rag_pipeline.handle_submission(_submission(invoice, "corr-baseline"))

    rag_agent = _RecordingAgent(StubAgent())
    rag_pipeline, _trust, publisher, _store = _make_pipeline(
        rag_agent, retriever=_policy_retriever()
    )
    result = await rag_pipeline.handle_submission(_submission(invoice, "corr-rag"))

    assert result.route == baseline.route
    assert result.retrieved_rules != []
    assert any(rid.startswith("MEAL-") for rid in result.retrieved_rules)

    topic, published = publisher.calls[0]
    assert topic == TOPIC_DECISION_MADE
    assert published["retrieved_rules"] == result.retrieved_rules

    # The agent double received the retriever's narrowed rendering, not the
    # full policy text -- and the no-RAG run received the full text.
    assert rag_agent.policy_rules_seen[0] != _POLICY_TEXT
    assert no_rag_agent.policy_rules_seen[0] == _POLICY_TEXT


async def test_rag_off_behaves_exactly_like_no_retriever():
    """RAG_ENABLED=false (even with a retriever injected) must fall back to
    the full policy text and empty retrieved_rules -- identical to a
    pipeline with no retriever at all."""
    import os

    invoice = _fixture("INV-1001")
    prior = os.environ.pop("RAG_ENABLED", None)
    try:
        os.environ["RAG_ENABLED"] = "false"
        agent = _RecordingAgent(StubAgent())
        pipeline, _trust, publisher, _store = _make_pipeline(agent, retriever=_policy_retriever())

        result = await pipeline.handle_submission(_submission(invoice))

        assert result.retrieved_rules == []
        assert agent.policy_rules_seen[0] == _POLICY_TEXT
        assert publisher.calls[0][1]["retrieved_rules"] == []
    finally:
        if prior is None:
            os.environ.pop("RAG_ENABLED", None)
        else:
            os.environ["RAG_ENABLED"] = prior


async def test_duplicate_has_empty_retrieved_rules_even_with_retriever(monkeypatch):
    """D-011: duplicates never reach the agent -- so there's nothing to
    retrieve for. retrieved_rules must be [] for a duplicate even when RAG
    is on and a retriever is wired in."""
    monkeypatch.setenv("RAG_ENABLED", "true")
    store = InMemoryStateStore()
    pipeline, _trust, publisher, _store = _make_pipeline(
        _RecordingAgent(StubAgent()), store=store, retriever=_policy_retriever()
    )

    inv1001, inv1007 = _fixture("INV-1001"), _fixture("INV-1007")
    await pipeline.handle_submission(_submission(inv1001))
    second = await pipeline.handle_submission(_submission(inv1007, correlation_id="corr-2"))

    assert second.route == "duplicate"
    assert second.retrieved_rules == []
    assert publisher.calls[1][1]["retrieved_rules"] == []


async def test_provider_unavailable_routes_human_review_agent_unavailable():
    pipeline, _trust, _publisher, _store = _make_pipeline(_FlakyAgent())
    invoice = _fixture("INV-1001")

    result = await pipeline.handle_submission(_submission(invoice))

    assert result.route == "human_review"
    assert "AGENT-UNAVAILABLE" in result.violations
    assert result.confidence is None
    assert result.recommendation == "n/a"


async def test_trusted_travel_vendor_320_auto_approves_above_base_ceiling():
    """D-012's uplift tier: a $320 travel invoice is over the $250 base
    ceiling but under the $400 trusted-uplift ceiling, and travel has no
    category cap -- so trust genuinely changes the outcome here (unlike a
    SaaS example, which would stay capped regardless)."""
    store = InMemoryStateStore()
    pipeline, trust, _publisher, _store = _make_pipeline(StubAgent(), store=store)
    vendor, category = "Global Airlines", "travel"
    invoice = {
        "vendor": vendor,
        "vendorKnown": True,
        "invoiceNumber": "GA-1001",
        "currency": "USD",
        "category": category,
        "lineItems": [{"description": "Economy flight", "quantity": 1, "unitPrice": 320.0}],
        "taxAmount": 0.0,
        "total": 320.0,
        "receiptPresent": True,
    }

    # Untrusted first: over the base ceiling -> human_review.
    untrusted_invoice = {**invoice, "invoiceNumber": "GA-0"}
    untrusted_payload = InvoiceSubmittedPayload(
        meta=new_event_meta("inv-untrusted", "corr-1"), invoice=untrusted_invoice
    )
    untrusted = await pipeline.handle_submission(untrusted_payload)
    assert untrusted.route == "human_review"
    assert untrusted.ceiling_cents == 25000

    await trust.record_paid(vendor, category)

    trusted = await pipeline.handle_submission(
        InvoiceSubmittedPayload(meta=new_event_meta("inv-trusted", "corr-2"), invoice=invoice)
    )
    assert trusted.route == "auto_approve"
    assert trusted.ceiling_cents == 40000
    assert trusted.usd_cents == 32000


async def test_payment_completed_marks_trust_for_the_decided_invoice():
    """Proves the invoice_id -> vendor/category cache written at decision
    time (`TrustRepo.remember_invoice`, called by the pipeline) round-trips
    through `record_paid_for_invoice` -- the mechanism the payment-completed
    handler uses, since `PaymentCompletedPayload` itself carries neither
    vendor nor category (payment stays pure money, D-014 point 7)."""
    store = InMemoryStateStore()
    pipeline, trust, _publisher, _store = _make_pipeline(StubAgent(), store=store)
    invoice = _fixture("INV-1002")  # saas, auto_approve, vendor "Atlassian"
    vendor, category = invoice["vendor"], invoice["category"]

    await pipeline.handle_submission(_submission(invoice))
    assert await trust.is_trusted(vendor, category) is False

    await trust.record_paid_for_invoice(invoice["id"])

    assert await trust.is_trusted(vendor, category) is True


async def test_record_paid_for_invoice_is_a_noop_for_unknown_invoice():
    """No submission was ever cached for this invoice_id -- must not raise."""
    _pipeline, trust, _publisher, _store = _make_pipeline(StubAgent())
    await trust.record_paid_for_invoice("inv-never-seen")  # must not raise


# --- Section 2: the actual /events/* HTTP handlers via TestClient ----------


def _cloudevent(payload: dict) -> dict:
    return {"specversion": "1.0", "type": "com.dapr.event.sent", "topic": "x", "data": payload}


@pytest.fixture
def env():
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    pipeline, trust, publisher, _ = _make_pipeline(StubAgent(), store=store)
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_pipeline] = lambda: pipeline
    app.dependency_overrides[deps.get_trust] = lambda: trust
    yield TestClient(app), pipeline, trust, publisher
    app.dependency_overrides.clear()


def test_subscribe_lists_the_two_topics(env):
    client, *_ = env
    subs = client.get("/dapr/subscribe").json()
    assert {s["topic"] for s in subs} == {"invoice-submitted", "payment-completed"}
    assert all(s["pubsubname"] == "pubsub" for s in subs)


def test_invoice_submitted_handler_processes_and_acks(env):
    client, _pipeline, _trust, publisher = env
    invoice = _fixture("INV-1001")
    body = _cloudevent(_submission(invoice).model_dump())

    resp = client.post("/events/invoice-submitted", json=body)

    assert resp.status_code == 200
    assert resp.json() == {"status": "SUCCESS"}
    assert len(publisher.calls) == 1
    assert publisher.calls[0][1]["route"] == "auto_approve"


def test_invoice_submitted_redelivered_event_is_acked_not_reprocessed(env):
    client, _pipeline, _trust, publisher = env
    invoice = _fixture("INV-1002")
    body = _cloudevent(_submission(invoice).model_dump())  # same event_id both times

    client.post("/events/invoice-submitted", json=body)
    resp = client.post("/events/invoice-submitted", json=body)

    assert resp.status_code == 200
    assert len(publisher.calls) == 1, "redelivery must not reprocess"


async def test_payment_completed_handler_marks_trust_end_to_end(env):
    client, _pipeline, trust, _publisher = env
    invoice = _fixture("INV-1017")  # hardware, auto_approve, known vendor
    vendor, category = invoice["vendor"], invoice["category"]

    client.post("/events/invoice-submitted", json=_cloudevent(_submission(invoice).model_dump()))
    assert await trust.is_trusted(vendor, category) is False

    pay_payload = PaymentCompletedPayload(
        meta=new_event_meta(invoice["id"], "corr-2"),
        amount_cents=18000,
        budget_remaining_cents=1000,
        department="engineering-2026Q2",
    ).model_dump()
    resp = client.post("/events/payment-completed", json=_cloudevent(pay_payload))

    assert resp.status_code == 200
    assert await trust.is_trusted(vendor, category) is True


def test_payment_completed_redelivered_event_is_acked_once_effect(env):
    client, _pipeline, _trust, _publisher = env
    invoice = _fixture("INV-1017")
    client.post("/events/invoice-submitted", json=_cloudevent(_submission(invoice).model_dump()))
    pay_payload = PaymentCompletedPayload(
        meta=new_event_meta(invoice["id"], "corr-2"),
        amount_cents=18000,
        budget_remaining_cents=1000,
        department="engineering-2026Q2",
    ).model_dump()
    body = _cloudevent(pay_payload)

    r1 = client.post("/events/payment-completed", json=body)
    r2 = client.post("/events/payment-completed", json=body)  # same event_id

    assert r1.status_code == 200 and r2.status_code == 200


class _FailOnceThenSucceedPublisher:
    """Raises on its first call -- simulating `publish(decision-made)`
    failing while the state store itself stays healthy, the canonical
    lost-decision case from the T7 review finding -- then succeeds on every
    call after."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raised_once = False

    async def __call__(self, topic: str, payload: dict) -> None:
        if not self._raised_once:
            self._raised_once = True
            raise RuntimeError("simulated publish failure")
        self.calls.append((topic, payload))


def test_redelivery_after_publish_failure_reprocesses_and_publishes_once():
    """THE KEY TEST for the T7 review fix: EventDedupe used to mark
    `processed:{event_id}` before `pipeline.handle_submission()` ran. If the
    pipeline then raised -- here, `publish(decision-made)` failing while the
    state store stays healthy -- the handler correctly 500'd, but the
    redelivered copy of the SAME event would just hit the stale mark and be
    acked as a duplicate, silently losing the decision forever.

    Proves the fix closes that: first delivery of a CloudEvent -> 500,
    nothing published; second delivery of the *identical* CloudEvent (same
    event_id) -> 200, and decision-made is published exactly once total
    (not zero -- the bug -- and not twice)."""
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    flaky_publisher = _FailOnceThenSucceedPublisher()
    pipeline, _trust, _publisher, _store = _make_pipeline(
        StubAgent(), publisher=flaky_publisher, store=store
    )
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_pipeline] = lambda: pipeline
    client = TestClient(app, raise_server_exceptions=False)
    try:
        invoice = _fixture("INV-1001")
        body = _cloudevent(_submission(invoice).model_dump())  # same event_id both times

        first = client.post("/events/invoice-submitted", json=body)
        assert first.status_code == 500
        assert len(flaky_publisher.calls) == 0, "must not falsely report success on failure"

        second = client.post("/events/invoice-submitted", json=body)
        assert second.status_code == 200
        assert len(flaky_publisher.calls) == 1, "decision-made must reach publisher exactly once"
    finally:
        app.dependency_overrides.clear()


def test_invoice_submitted_infra_failure_propagates_500():
    """When the pipeline itself blows up on an infra failure (state store
    unreachable), the handler must NOT swallow it into a 200 ack -- Dapr
    needs the 500 to redeliver."""

    class _BrokenStateStore(InMemoryStateStore):
        async def get(self, key: str):
            raise RuntimeError("state store unreachable")

    store = _BrokenStateStore()
    dedupe = EventDedupe(InMemoryStateStore())  # dedupe itself must succeed
    pipeline, _trust, _publisher, _ = _make_pipeline(StubAgent(), store=store)
    app.dependency_overrides[deps.get_dedupe] = lambda: dedupe
    app.dependency_overrides[deps.get_pipeline] = lambda: pipeline
    client = TestClient(app, raise_server_exceptions=False)
    try:
        invoice = _fixture("INV-1001")
        body = _cloudevent(_submission(invoice).model_dump())

        resp = client.post("/events/invoice-submitted", json=body)

        assert resp.status_code == 500
    finally:
        app.dependency_overrides.clear()
