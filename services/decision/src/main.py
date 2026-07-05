from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

from .config_api import router as config_router
from .subscriptions import router as subscriptions_router

setup_json_logging("decision-svc")

app = FastAPI(title="ApprovalFlow — Decision Service", version="0.1.0")
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "decision-svc"}


app.include_router(config_router)
app.include_router(subscriptions_router)
