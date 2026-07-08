import logging
from contextlib import asynccontextmanager

from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

from .api import router as api_router
from .deps import get_budget_store
from .subscriptions import router as subscriptions_router

setup_json_logging("payment-svc")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort startup seed. Every BudgetStore access (reserve/release/
    # get_remaining) also seeds lazily, so seeding here is optional — and it
    # MUST NOT be fatal: the app and its Dapr sidecar have a circular startup
    # dependency (the sidecar's network namespace is the app's; the sidecar
    # only starts once the app is healthy), so at lifespan time the sidecar's
    # state API is typically not yet reachable. Crashing here would wedge boot
    # entirely (app exits → never healthy → sidecar never starts). Log and
    # continue; the lazy seed covers the first real access once Dapr is up.
    try:
        await get_budget_store().seed_if_absent()
    except Exception:
        log.warning("startup budget seed deferred: dapr sidecar not ready yet; "
                    "budgets will seed lazily on first access")
    yield


app = FastAPI(title="ApprovalFlow — Payment Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "payment-svc"}


app.include_router(api_router)
app.include_router(subscriptions_router)
