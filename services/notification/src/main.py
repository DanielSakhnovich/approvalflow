from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

from .api import router as api_router
from .subscriptions import router as subscriptions_router

setup_json_logging("notification-svc")

# D-006: the vendored HW3 domain (storage/processor/segmenter/providers) is
# reused unchanged. Sample-data seeding is deliberately NOT run here -- the
# running service starts empty; storage.seed() exists only for the original
# standalone app. In-memory storage is the one documented statefulness
# exception (D-010): notifications are transient.

app = FastAPI(title="ApprovalFlow — Notification Service", version="0.1.0")
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "notification-svc"}


app.include_router(api_router)
app.include_router(subscriptions_router)
