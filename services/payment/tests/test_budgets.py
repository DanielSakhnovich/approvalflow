import asyncio

from afcommon.state import InMemoryStateStore, YieldingStateStore

from services.payment.src.budgets import BudgetStore, to_cents

_MARKETING = "marketing-2026Q2"
_ENGINEERING = "engineering-2026Q2"
_SALES = "sales-2026Q2"


async def test_seed_creates_all_fixture_budgets_in_cents():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.get_remaining(_MARKETING) == 100000
    assert await store.get_remaining(_ENGINEERING) == 5000000
    assert await store.get_remaining(_SALES) == 2000000


async def test_seed_is_idempotent_and_never_resets_spent_budget():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()

    assert await store.reserve(_MARKETING, 40000) is True
    assert await store.get_remaining(_MARKETING) == 60000

    # Re-seeding (simulating a restart) must not clobber the spent budget.
    await store.seed_if_absent()
    assert await store.get_remaining(_MARKETING) == 60000


async def test_reserve_success_decrements_remaining():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.reserve(_MARKETING, 30000) is True
    assert await store.get_remaining(_MARKETING) == 70000


async def test_reserve_insufficient_returns_false_and_budget_unchanged():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.reserve(_MARKETING, 100001) is False
    assert await store.get_remaining(_MARKETING) == 100000


async def test_reserve_exact_remaining_succeeds_to_zero():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.reserve(_MARKETING, 100000) is True
    assert await store.get_remaining(_MARKETING) == 0


async def test_release_restores_budget():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.reserve(_MARKETING, 40000) is True
    await store.release(_MARKETING, 40000)
    assert await store.get_remaining(_MARKETING) == 100000


async def test_reserve_unknown_department_returns_false_no_crash():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.reserve("nonexistent-dept", 100) is False


async def test_release_unknown_department_no_crash():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    # Must not raise even though the department was never seeded.
    await store.release("nonexistent-dept", 100)


async def test_get_remaining_unknown_department_returns_none():
    store = BudgetStore(InMemoryStateStore())
    await store.seed_if_absent()
    assert await store.get_remaining("nonexistent-dept") is None


async def test_concurrent_reserves_exactly_one_succeeds_no_overspend():
    """INV-1014 core: two concurrent 60000-cent reserves against a 100000
    budget on a genuinely-suspending store must yield exactly one success,
    remaining exactly 40000, and never go negative."""
    store = BudgetStore(YieldingStateStore())
    await store.seed_if_absent()

    results = await asyncio.gather(
        store.reserve(_MARKETING, 60000),
        store.reserve(_MARKETING, 60000),
    )

    assert sorted(results) == [False, True]
    remaining = await store.get_remaining(_MARKETING)
    assert remaining == 40000
    assert remaining >= 0


def test_to_cents_converts_dollars_to_cents():
    assert to_cents(1000.0) == 100000
    assert to_cents(50000.0) == 5000000
    assert to_cents(20000.0) == 2000000


def test_to_cents_rounds_half_up():
    assert to_cents(9.995) == 1000
    assert to_cents(0.005) == 1
