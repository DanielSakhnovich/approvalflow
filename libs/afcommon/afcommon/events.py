import uuid
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

PUBSUB_NAME = "pubsub"

TOPIC_INVOICE_SUBMITTED = "invoice-submitted"
TOPIC_DECISION_MADE = "decision-made"
TOPIC_APPROVAL_RESOLVED = "approval-resolved"
TOPIC_PAYMENT_COMPLETED = "payment-completed"
TOPIC_PAYMENT_FAILED = "payment-failed"


class EventMeta(BaseModel):
    event_id: str
    invoice_id: str
    correlation_id: str
    occurred_at: str


def new_event_meta(invoice_id: str, correlation_id: str) -> EventMeta:
    return EventMeta(
        event_id=uuid.uuid4().hex,
        invoice_id=invoice_id,
        correlation_id=correlation_id,
        occurred_at=datetime.now(UTC).isoformat(),
    )


async def publish(
    topic: str,
    payload: dict,
    base_url: str = "http://localhost:3500",
    client: httpx.AsyncClient | None = None,
) -> None:
    if client is not None:
        resp = await client.post(f"{base_url}/v1.0/publish/{PUBSUB_NAME}/{topic}", json=payload)
        resp.raise_for_status()
        return
    async with httpx.AsyncClient(timeout=10.0) as owned_client:
        url = f"{base_url}/v1.0/publish/{PUBSUB_NAME}/{topic}"
        resp = await owned_client.post(url, json=payload)
        resp.raise_for_status()
