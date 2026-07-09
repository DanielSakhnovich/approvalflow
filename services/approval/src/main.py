import os

from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

from .api import router as api_router
from .subscriptions import router as subscriptions_router

setup_json_logging("approval-svc")

# Fail loud at startup, not on first request: if auth enforcement is turned
# on but no signing secret is configured, refuse to boot rather than 500ing
# the first time a protected route is hit (afcommon.auth's require_role
# would raise the same RuntimeError lazily, per-request, otherwise).
if os.environ.get("AUTH_ENABLED", "").lower() == "true" and not os.environ.get(
    "JWT_SIGNING_SECRET"
):
    raise RuntimeError(
        "AUTH_ENABLED is set but JWT_SIGNING_SECRET is not configured -- "
        "refusing to start approval-svc with auth enforced and no signing secret."
    )

app = FastAPI(title="ApprovalFlow — Approval Service", version="0.1.0")
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "approval-svc"}


app.include_router(api_router)
app.include_router(subscriptions_router)
