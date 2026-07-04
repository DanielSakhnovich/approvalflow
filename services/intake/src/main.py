from afcommon.logging import setup_json_logging
from afcommon.middleware import CorrelationIdMiddleware
from fastapi import FastAPI

setup_json_logging("intake-api")

app = FastAPI(title="ApprovalFlow — Intake API", version="0.1.0")
app.add_middleware(CorrelationIdMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "intake-api"}
