import json

import httpx
import pytest
from afcommon.events import (
    TOPIC_INVOICE_SUBMITTED,
    EventMeta,
    new_event_meta,
    publish,
)


def test_topics_are_dash_named():
    assert TOPIC_INVOICE_SUBMITTED == "invoice-submitted"


def test_new_event_meta_fields():
    meta = new_event_meta(invoice_id="INV-1001", correlation_id="corr-1")
    assert meta.invoice_id == "INV-1001"
    assert meta.correlation_id == "corr-1"
    assert len(meta.event_id) == 32          # uuid4 hex
    assert meta.occurred_at.endswith("+00:00")


def test_event_ids_are_unique():
    a = new_event_meta("i", "c")
    b = new_event_meta("i", "c")
    assert a.event_id != b.event_id


def test_meta_roundtrips_via_dict():
    meta = new_event_meta("INV-1", "corr-1")
    assert EventMeta(**meta.model_dump()) == meta


async def test_publish_posts_payload_as_json_to_expected_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    payload = {"invoice_id": "INV-1", "amount": 100}

    await publish("invoice-submitted", payload, client=client)

    assert captured["url"] == "http://localhost:3500/v1.0/publish/pubsub/invoice-submitted"
    assert json.loads(captured["body"]) == payload


async def test_publish_non_2xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errorCode": "ERR_PUBSUB_PUBLISH_MESSAGE"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        await publish("invoice-submitted", {"a": 1}, client=client)
