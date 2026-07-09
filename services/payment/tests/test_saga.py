"""Task 3: PaymentSaga -- the money-consistency core (M9/M10/M12, journey D).

Per-invoice idempotency via the durable `saga:{invoice_id}` record is THE
at-least-once guarantee: a terminal record short-circuits handle() with no
re-effect and no re-publish, covering both a plain retry AND a genuinely
distinct redelivery (fresh event/correlation ids) of the same invoice --
approval-resolved is at-least-once per invoice, not per event_id.
"""

import logging

import pytest
from afcommon.state import InMemoryStateStore

from services.payment.src.budgets import BudgetStore
from services.payment.src.provider import MockPaymentProvider
from services.payment.src.saga import PaymentSaga, SagaState

# Seeded to 5,000,000 cents ($50,000) by sample-invoices.json.
_DEPT = "engineering-2026Q2"


class _CapturingPublisher:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, topic: str, payload: dict) -> None:
        self.events.append((topic, payload))


class _CountingBudgetStore(BudgetStore):
    """Spies on reserve/release calls so tests can assert exactly-once
    effects instead of only the net budget delta."""

    def __init__(self, store):
        super().__init__(store)
        self.reserve_calls = 0
        self.release_calls = 0

    async def reserve(self, dept, amount_cents):
        self.reserve_calls += 1
        return await super().reserve(dept, amount_cents)

    async def release(self, dept, amount_cents):
        self.release_calls += 1
        return await super().release(dept, amount_cents)


class _CountingProvider(MockPaymentProvider):
    def __init__(self, store, injection_enabled):
        super().__init__(store, injection_enabled)
        self.execute_calls = 0

    async def execute(self, invoice_id, amount_cents, scenario):
        self.execute_calls += 1
        return await super().execute(invoice_id, amount_cents, scenario)


async def _make_saga(*, injection_enabled=False, raw_store=None):
    raw_store = raw_store if raw_store is not None else InMemoryStateStore()
    budgets = _CountingBudgetStore(raw_store)
    await budgets.seed_if_absent()
    provider = _CountingProvider(raw_store, injection_enabled)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, raw_store, publisher)
    return saga, budgets, provider, publisher, raw_store


async def test_happy_auto_path_reserves_executes_and_pays():
    saga, budgets, provider, publisher, _ = await _make_saga()
    before = await budgets.get_remaining(_DEPT)

    record = await saga.handle(
        "inv-1", "corr-1", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=100000,
    )

    assert record.state == SagaState.paid
    assert record.payment_ref
    assert record.failure_reason is None
    assert budgets.reserve_calls == 1
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000

    assert len(publisher.events) == 1
    topic, payload = publisher.events[0]
    assert topic == "payment-completed"
    assert payload["meta"]["invoice_id"] == "inv-1"
    assert payload["meta"]["correlation_id"] == "corr-1"
    assert payload["amount_cents"] == 40000
    assert payload["budget_remaining_cents"] == before - 40000
    assert payload["department"] == _DEPT


async def test_human_path_skips_ceiling_check_even_above_ceiling():
    """Human approval IS the authorization: auto_route=False must pay even
    when amount_cents > ceiling_cents."""
    saga, budgets, provider, publisher, _ = await _make_saga()

    record = await saga.handle(
        "inv-2", "corr-1", _DEPT, 200000, "",
        auto_route=False, ceiling_cents=100000,
    )

    assert record.state == SagaState.paid
    assert record.failure_reason is None
    assert provider.execute_calls == 1
    topic, _payload = publisher.events[0]
    assert topic == "payment-completed"


async def test_ceiling_violation_on_auto_refuses_loudly_without_touching_budget(caplog):
    """M12 layer 4: auto_route AND amount_cents > ceiling_cents must refuse
    without ever calling reserve/execute, and log critical."""
    saga, budgets, provider, publisher, _ = await _make_saga()
    before = await budgets.get_remaining(_DEPT)

    with caplog.at_level(logging.CRITICAL):
        record = await saga.handle(
            "inv-3", "corr-1", _DEPT, 200000, "",
            auto_route=True, ceiling_cents=100000,
        )

    assert record.state == SagaState.compensated
    assert record.failure_reason == "ceiling-violation"
    assert record.payment_ref is None
    assert budgets.reserve_calls == 0
    assert provider.execute_calls == 0
    assert await budgets.get_remaining(_DEPT) == before

    assert len(publisher.events) == 1
    topic, payload = publisher.events[0]
    assert topic == "payment-failed"
    assert payload["reason"] == "ceiling-violation"
    assert payload["compensated"] is False

    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("inv-3" in r.getMessage() for r in critical)


async def test_ceiling_boundary_equal_to_ceiling_passes_and_pays():
    """M12 layer 4 guard is a strict `>` (see saga.py step 2): an auto-routed
    amount exactly EQUAL to ceiling_cents must NOT trip the ceiling refusal
    -- it proceeds through reserve/execute/pay like any in-ceiling amount."""
    saga, budgets, provider, publisher, _ = await _make_saga()
    before = await budgets.get_remaining(_DEPT)

    record = await saga.handle(
        "inv-11", "corr-1", _DEPT, 100000, "",
        auto_route=True, ceiling_cents=100000,
    )

    assert record.state == SagaState.paid
    assert record.failure_reason is None
    assert budgets.reserve_calls == 1
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 100000

    assert len(publisher.events) == 1
    assert publisher.events[0][0] == "payment-completed"


async def test_insufficient_budget_rejects_and_leaves_budget_unchanged():
    saga, budgets, provider, publisher, _ = await _make_saga()
    before = await budgets.get_remaining(_DEPT)

    record = await saga.handle(
        "inv-4", "corr-1", _DEPT, before + 1, "",
        auto_route=True, ceiling_cents=None,
    )

    assert record.state == SagaState.rejected_insufficient_budget
    assert record.failure_reason == "insufficient_budget"
    assert record.payment_ref is None
    assert await budgets.get_remaining(_DEPT) == before
    assert provider.execute_calls == 0

    assert len(publisher.events) == 1
    topic, payload = publisher.events[0]
    assert topic == "payment-failed"
    assert payload["reason"] == "insufficient_budget"
    assert payload["compensated"] is False


async def test_declined_payment_compensates_and_restores_budget_exactly():
    """Journey D: reserve -> provider declines -> release -> compensated.
    No orphaned reservation."""
    saga, budgets, provider, publisher, _ = await _make_saga(injection_enabled=True)
    before = await budgets.get_remaining(_DEPT)

    record = await saga.handle(
        "inv-5", "corr-1", _DEPT, 40000, "payment-failure:journey-D",
        auto_route=True, ceiling_cents=None,
    )

    assert record.state == SagaState.compensated
    assert record.payment_ref is None
    assert budgets.reserve_calls == 1
    assert budgets.release_calls == 1
    assert await budgets.get_remaining(_DEPT) == before  # restored EXACTLY

    assert len(publisher.events) == 1
    topic, payload = publisher.events[0]
    assert topic == "payment-failed"
    assert payload["compensated"] is True


async def test_terminal_idempotency_second_handle_returns_cached_no_second_effect():
    saga, budgets, provider, publisher, _ = await _make_saga()

    first = await saga.handle(
        "inv-6", "corr-1", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )
    second = await saga.handle(
        "inv-6", "corr-2", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )

    assert second == first
    assert budgets.reserve_calls == 1
    assert provider.execute_calls == 1
    assert len(publisher.events) == 1


async def test_double_approval_resolved_simulation_exactly_one_payment():
    """THE BINDING at-least-once test: two handle() calls for the same
    invoice_id, each with a DIFFERENT event/correlation context (simulating
    approval-resolved's at-least-once-per-invoice redelivery), must still
    produce exactly one payment and one payment-completed event."""
    saga, budgets, provider, publisher, _ = await _make_saga()
    before = await budgets.get_remaining(_DEPT)

    first = await saga.handle(
        "inv-7", "corr-a", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )
    second = await saga.handle(
        "inv-7", "corr-b", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )

    assert first.state == SagaState.paid
    assert second == first
    assert budgets.reserve_calls == 1
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000
    assert len(publisher.events) == 1
    assert publisher.events[0][0] == "payment-completed"


async def test_crash_resume_from_reserved_executes_without_double_reserve():
    """Simulates a crash after reserve() succeeded but before provider.execute()
    ran: a 'reserved' marker is pre-seeded directly (bypassing handle()).
    handle() must resume at step 4 (execute) without reserving again --
    provider idempotency alone must not be relied on to prevent overspend."""
    raw_store = InMemoryStateStore()
    budgets = _CountingBudgetStore(raw_store)
    await budgets.seed_if_absent()
    before = await budgets.get_remaining(_DEPT)
    assert await budgets.reserve(_DEPT, 40000) is True  # the pre-crash reserve
    budgets.reserve_calls = 0  # reset: handle() itself must not reserve again

    provider = _CountingProvider(raw_store, injection_enabled=False)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, raw_store, publisher)

    await raw_store.try_save(
        "saga:inv-8",
        {
            "invoice_id": "inv-8",
            "correlation_id": "corr-1",
            "state": "reserved",
            "department": _DEPT,
            "amount_cents": 40000,
            "payment_ref": None,
            "failure_reason": None,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        None,
    )

    record = await saga.handle(
        "inv-8", "corr-2", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )

    assert record.state == SagaState.paid
    assert budgets.reserve_calls == 0
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000  # decremented ONCE


async def test_crash_resume_from_started_reserves_then_pays():
    """A 'started' marker pre-seeded directly (crash before reserve() ran)
    must resume at step 3: reserve, then execute, then pay."""
    raw_store = InMemoryStateStore()
    budgets = _CountingBudgetStore(raw_store)
    await budgets.seed_if_absent()
    before = await budgets.get_remaining(_DEPT)

    provider = _CountingProvider(raw_store, injection_enabled=False)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, raw_store, publisher)

    await raw_store.try_save(
        "saga:inv-9",
        {
            "invoice_id": "inv-9",
            "correlation_id": "corr-1",
            "state": "started",
            "department": _DEPT,
            "amount_cents": 40000,
            "payment_ref": None,
            "failure_reason": None,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        None,
    )

    record = await saga.handle(
        "inv-9", "corr-2", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )

    assert record.state == SagaState.paid
    assert budgets.reserve_calls == 1
    assert provider.execute_calls == 1
    assert await budgets.get_remaining(_DEPT) == before - 40000


@pytest.mark.parametrize("terminal_state", ["paid", "compensated", "rejected_insufficient_budget"])
async def test_any_terminal_state_short_circuits_untouched(terminal_state):
    raw_store = InMemoryStateStore()
    budgets = _CountingBudgetStore(raw_store)
    await budgets.seed_if_absent()
    provider = _CountingProvider(raw_store, injection_enabled=False)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, raw_store, publisher)

    seeded = {
        "invoice_id": "inv-10",
        "correlation_id": "corr-1",
        "state": terminal_state,
        "department": _DEPT,
        "amount_cents": 40000,
        "payment_ref": "pay_existing" if terminal_state == "paid" else None,
        "failure_reason": None if terminal_state == "paid" else "some-reason",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    await raw_store.try_save("saga:inv-10", seeded, None)

    record = await saga.handle(
        "inv-10", "corr-2", _DEPT, 40000, "",
        auto_route=True, ceiling_cents=None,
    )

    assert record.state == terminal_state
    assert record.model_dump() == seeded
    assert budgets.reserve_calls == 0
    assert provider.execute_calls == 0
    assert publisher.events == []


async def test_ceiling_guard_skipped_on_reserved_resume_no_orphaned_reservation():
    """Finding 2: a redelivery that finds the saga already at `reserved` must
    NOT hit the ceiling guard even if auto_route/ceiling_cents on THIS call
    would otherwise violate it -- the reservation already happened, so
    writing `compensated` here (without releasing) would orphan it. The
    guard only applies before a reservation exists."""
    raw_store = InMemoryStateStore()
    budgets = _CountingBudgetStore(raw_store)
    await budgets.seed_if_absent()
    before = await budgets.get_remaining(_DEPT)
    assert await budgets.reserve(_DEPT, 200000) is True  # pre-crash reserve, above ceiling
    budgets.reserve_calls = 0

    provider = _CountingProvider(raw_store, injection_enabled=False)
    publisher = _CapturingPublisher()
    saga = PaymentSaga(budgets, provider, raw_store, publisher)

    await raw_store.try_save(
        "saga:inv-12",
        {
            "invoice_id": "inv-12",
            "correlation_id": "corr-1",
            "state": "reserved",
            "department": _DEPT,
            "amount_cents": 200000,
            "payment_ref": None,
            "failure_reason": None,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        None,
    )

    # This call's own ceiling (100000) would violate against amount_cents
    # 200000 -- but the saga is already reserved, so it must resume forward.
    record = await saga.handle(
        "inv-12", "corr-2", _DEPT, 200000, "",
        auto_route=True, ceiling_cents=100000,
    )

    assert record.state == SagaState.paid
    assert record.failure_reason is None
    assert budgets.reserve_calls == 0  # never re-reserved
    assert budgets.release_calls == 0  # nothing to release, resumed forward instead
    # reservation honored, not orphaned
    assert await budgets.get_remaining(_DEPT) == before - 200000
    assert len(publisher.events) == 1
    assert publisher.events[0][0] == "payment-completed"


async def test_zero_amount_never_strands_at_started_compensates_invalid_amount(caplog):
    """Finding 3: reserve() raises ValueError on amount_cents<=0. handle()
    must guard BEFORE claiming/creating a `started` record, so a poison
    0-cent payload never strands the saga -- it goes straight to
    `compensated` with reason=invalid_amount, no reserve/execute attempted."""
    saga, budgets, provider, publisher, raw_store = await _make_saga()

    with caplog.at_level(logging.CRITICAL):
        record = await saga.handle(
            "inv-13", "corr-1", _DEPT, 0, "",
            auto_route=True, ceiling_cents=None,
        )

    assert record.state == SagaState.compensated
    assert record.failure_reason == "invalid_amount"
    assert record.payment_ref is None
    assert budgets.reserve_calls == 0
    assert provider.execute_calls == 0

    value, _ = await raw_store.get("saga:inv-13")
    assert value["state"] == "compensated"  # never observed at `started`

    assert len(publisher.events) == 1
    topic, payload = publisher.events[0]
    assert topic == "payment-failed"
    assert payload["reason"] == "invalid_amount"
    assert payload["compensated"] is False

    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("inv-13" in r.getMessage() for r in critical)
