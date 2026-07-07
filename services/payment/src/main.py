from contextlib import asynccontextmanager

from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from afcommon.state import DaprStateStore
from fastapi import FastAPI

from .budgets import BudgetStore

setup_json_logging("payment-svc")

_budget_store = BudgetStore(DaprStateStore())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Belt-and-braces: BudgetStore.seed_if_absent() is also called lazily
    # before the first reserve(), in case tests/callers hit the store
    # without ever running this startup hook (e.g. bare TestClient(app)
    # never triggers ASGI lifespan events).
    await _budget_store.seed_if_absent()
    yield


app = FastAPI(title="ApprovalFlow — Payment Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "payment-svc"}
