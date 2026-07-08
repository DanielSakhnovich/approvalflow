import os

from afcommon.dedupe import EventDedupe
from afcommon.events import publish
from afcommon.state import DaprStateStore

from .budgets import BudgetStore
from .provider import MockPaymentProvider
from .saga import PaymentSaga

_budget_store: BudgetStore | None = None
_dedupe: EventDedupe | None = None
_provider: MockPaymentProvider | None = None
_saga: PaymentSaga | None = None


def get_budget_store() -> BudgetStore:
    global _budget_store
    if _budget_store is None:
        _budget_store = BudgetStore(DaprStateStore())
    return _budget_store


def get_dedupe() -> EventDedupe:
    global _dedupe
    if _dedupe is None:
        _dedupe = EventDedupe(DaprStateStore())
    return _dedupe


def get_provider() -> MockPaymentProvider:
    """Lazy singleton. `FAILURE_INJECTION_ENABLED` is read once, at build
    time -- like `AGENT_PROVIDER` in decision-svc's `_build_agent` -- so the
    harness's fixture-driven payment-decline scenarios (M12/journey D) are
    controlled by process env at startup, not re-read per call."""
    global _provider
    if _provider is None:
        injection_enabled = os.environ.get("FAILURE_INJECTION_ENABLED") == "true"
        _provider = MockPaymentProvider(DaprStateStore(), injection_enabled)
    return _provider


def get_saga() -> PaymentSaga:
    global _saga
    if _saga is None:
        _saga = PaymentSaga(get_budget_store(), get_provider(), DaprStateStore(), publish)
    return _saga
