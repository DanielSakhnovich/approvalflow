from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

from .api import router as api_router

setup_json_logging("audit-svc")

app = FastAPI(title="ApprovalFlow — Audit Service", version="0.1.0")
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "audit-svc"}


app.include_router(api_router)
