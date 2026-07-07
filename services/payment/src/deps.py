from afcommon.state import DaprStateStore

from .budgets import BudgetStore

_budget_store: BudgetStore | None = None


def get_budget_store() -> BudgetStore:
    global _budget_store
    if _budget_store is None:
        _budget_store = BudgetStore(DaprStateStore())
    return _budget_store
