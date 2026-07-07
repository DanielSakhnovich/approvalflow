import asyncio

import pytest
from afcommon.contracts import DecisionMadePayload
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore, YieldingStateStore

from services.approval.src.models import Escalation, EscalationStatus
from services.approval.src.repo import ApprovalRepo


@pytest.fixture
def invoice_id():
    return "inv-123"


@pytest.fixture
def correlation_id():
    return "corr-456"


@pytest.fixture
def event_meta(invoice_id, correlation_id):
    return new_event_meta(invoice_id, correlation_id)


@pytest.fixture
def decision_payload(event_meta):
    return DecisionMadePayload(
        meta=event_meta,
        route="human_review",
        recommendation="Escalate to management",
        confidence=0.75,
        violations=["policy_a", "policy_b"],
        reasoning="High value and policy violation",
        usd_cents=50000,
        ceiling_cents=100000,
        scenario="payment-failure:journey-D",
        department="engineering-2026Q2",
    )


@pytest.fixture
def escalation(event_meta, invoice_id, correlation_id):
    return Escalation(
        invoice_id=invoice_id,
        correlation_id=correlation_id,
        status=EscalationStatus.pending,
        usd_cents=50000,
        route_violations=["policy_a", "policy_b"],
        recommendation="Escalate to management",
        confidence=0.75,
        reasoning="High value and policy violation",
        submitter="",
        vendor="",
        category="",
        escalated_at=event_meta.occurred_at,
    )


class TestSaveNew:
    async def test_save_new_returns_true_first_time(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        result = await repo.save_new(escalation)
        assert result is True

    async def test_save_new_returns_false_second_time(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)
        result = await repo.save_new(escalation)
        assert result is False

    async def test_save_new_idempotent_record_unchanged(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)
        # Try to save again with a modified version
        modified = escalation.model_copy(update={"status": EscalationStatus.approved})
        await repo.save_new(modified)
        # Original should still be in store
        retrieved = await repo.get(escalation.invoice_id)
        assert retrieved.status == EscalationStatus.pending


class TestGet:
    async def test_get_existing_escalation(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)
        retrieved = await repo.get(escalation.invoice_id)
        assert retrieved is not None
        assert retrieved.invoice_id == escalation.invoice_id
        assert retrieved.status == EscalationStatus.pending

    async def test_get_nonexistent_escalation_returns_none(self):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        result = await repo.get("nonexistent")
        assert result is None


class TestResolve:
    async def test_resolve_updates_status(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)

        def transform(esc: Escalation) -> Escalation:
            return esc.model_copy(update={"status": EscalationStatus.approved})

        resolved = await repo.resolve(escalation.invoice_id, transform)
        assert resolved.status == EscalationStatus.approved

    async def test_resolve_updates_multiple_fields(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)

        def transform(esc: Escalation) -> Escalation:
            return esc.model_copy(
                update={
                    "status": EscalationStatus.approved,
                    "resolved_by": "approver-1",
                    "resolution_comment": "Approved by policy",
                }
            )

        resolved = await repo.resolve(escalation.invoice_id, transform)
        assert resolved.status == EscalationStatus.approved
        assert resolved.resolved_by == "approver-1"
        assert resolved.resolution_comment == "Approved by policy"

    async def test_resolve_missing_raises_key_error(self):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)

        def transform(esc: Escalation) -> Escalation:
            return esc

        with pytest.raises(KeyError):
            await repo.resolve("nonexistent", transform)

    async def test_resolve_persists_changes(self, escalation):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.save_new(escalation)

        def transform(esc: Escalation) -> Escalation:
            return esc.model_copy(update={"status": EscalationStatus.rejected})

        await repo.resolve(escalation.invoice_id, transform)
        retrieved = await repo.get(escalation.invoice_id)
        assert retrieved.status == EscalationStatus.rejected


class TestQueue:
    async def test_add_to_queue_idempotent_first_add(self, invoice_id):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.add_to_queue(invoice_id)
        queue = await repo.list_queue()
        assert queue == [invoice_id]

    async def test_add_to_queue_idempotent_duplicate(self, invoice_id):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.add_to_queue(invoice_id)
        await repo.add_to_queue(invoice_id)
        queue = await repo.list_queue()
        assert queue == [invoice_id]
        assert len(queue) == 1

    async def test_remove_from_queue_idempotent(self, invoice_id):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.add_to_queue(invoice_id)
        await repo.remove_from_queue(invoice_id)
        queue = await repo.list_queue()
        assert queue == []

    async def test_remove_from_queue_idempotent_when_not_present(self, invoice_id):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.remove_from_queue(invoice_id)
        queue = await repo.list_queue()
        assert queue == []

    async def test_list_queue_empty(self):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        queue = await repo.list_queue()
        assert queue == []

    async def test_list_queue_multiple_items(self):
        store = InMemoryStateStore()
        repo = ApprovalRepo(store)
        await repo.add_to_queue("inv-1")
        await repo.add_to_queue("inv-2")
        await repo.add_to_queue("inv-3")
        queue = await repo.list_queue()
        assert queue == ["inv-1", "inv-2", "inv-3"]

    async def test_concurrent_add_to_queue_no_lost_update(self):
        store = YieldingStateStore()
        repo = ApprovalRepo(store)

        # Two concurrent adds on the same store
        async def add_both():
            await asyncio.gather(
                repo.add_to_queue("inv-1"),
                repo.add_to_queue("inv-2"),
            )

        await add_both()
        queue = await repo.list_queue()
        assert set(queue) == {"inv-1", "inv-2"}
        assert len(queue) == 2


class TestFromDecision:
    """The wire-facing constructor Task 2's subscriber depends on."""

    def test_maps_payload_fields(self, decision_payload, invoice_id, correlation_id):
        esc = Escalation.from_decision(decision_payload)
        assert esc.invoice_id == invoice_id
        assert esc.correlation_id == correlation_id
        assert esc.status == EscalationStatus.pending
        assert esc.usd_cents == 50000
        assert esc.route_violations == ["policy_a", "policy_b"]
        assert esc.recommendation == "Escalate to management"
        assert esc.confidence == 0.75
        assert esc.reasoning == "High value and policy violation"
        assert esc.resolved_at is None and esc.resolved_by is None
        # escalated_at comes from the event meta: ISO 8601 with UTC offset
        assert esc.escalated_at.endswith("+00:00")
        # scenario/department ride the wire payload itself (Phase 05), not
        # the optional invoice-enrichment arg.
        assert esc.scenario == "payment-failure:journey-D"
        assert esc.department == "engineering-2026Q2"

    def test_display_fields_empty_without_invoice(self, decision_payload):
        esc = Escalation.from_decision(decision_payload)
        assert (esc.vendor, esc.submitter, esc.category) == ("", "", "")

    def test_invoice_enrichment_populates_display_fields(self, decision_payload):
        esc = Escalation.from_decision(
            decision_payload,
            invoice={"vendor": "Bistro 19", "submitter": "dana@northwind.example",
                     "category": "meals"},
        )
        assert esc.vendor == "Bistro 19"
        assert esc.submitter == "dana@northwind.example"
        assert esc.category == "meals"
