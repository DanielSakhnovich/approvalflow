from services.payment.src import deps
from services.payment.src.budgets import BudgetStore


def test_get_budget_store_returns_budget_store():
    assert isinstance(deps.get_budget_store(), BudgetStore)


def test_get_budget_store_is_overridable():
    sentinel = object()
    original = deps._budget_store
    try:
        deps._budget_store = sentinel
        assert deps.get_budget_store() is sentinel
    finally:
        deps._budget_store = original
