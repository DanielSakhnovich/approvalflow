"""
D-016: EventDedupe / bind_event_context / parse_cloudevent -- the mechanical
consumer-dedupe wrapper extracted once into afcommon (see decisions.md D-016).
"""

import pytest
from afcommon.dedupe import EventDedupe, bind_event_context, parse_cloudevent
from afcommon.events import EventMeta, new_event_meta
from afcommon.logging import correlation_id_var, invoice_id_var
from afcommon.state import InMemoryStateStore
from pydantic import BaseModel


class _Payload(BaseModel):
    meta: EventMeta
    amount: int


# --- EventDedupe -------------------------------------------------------


async def test_first_time_true_exactly_once():
    dedupe = EventDedupe(InMemoryStateStore())
    assert await dedupe.first_time("evt-1") is True
    assert await dedupe.first_time("evt-1") is False
    assert await dedupe.first_time("evt-2") is True


async def test_first_time_uses_processed_prefix_key():
    store = InMemoryStateStore()
    dedupe = EventDedupe(store)
    await dedupe.first_time("evt-42")
    value, _ = await store.get("processed:evt-42")
    assert value == {"seen": True}


# --- bind_event_context --------------------------------------------------


def test_bind_event_context_from_event_meta():
    meta = new_event_meta("inv-1", "corr-1")
    bind_event_context(meta)
    assert correlation_id_var.get() == "corr-1"
    assert invoice_id_var.get() == "inv-1"


def test_bind_event_context_from_plain_dict():
    bind_event_context({"correlation_id": "corr-2", "invoice_id": "inv-2"})
    assert correlation_id_var.get() == "corr-2"
    assert invoice_id_var.get() == "inv-2"


def test_bind_event_context_rebinds_on_each_call():
    bind_event_context(new_event_meta("inv-a", "corr-a"))
    assert invoice_id_var.get() == "inv-a"
    bind_event_context(new_event_meta("inv-b", "corr-b"))
    assert invoice_id_var.get() == "inv-b"
    assert correlation_id_var.get() == "corr-b"


# --- parse_cloudevent ------------------------------------------------------


def test_parse_cloudevent_unwraps_data_into_model():
    meta = new_event_meta("inv-3", "corr-3")
    body = {
        "specversion": "1.0",
        "type": "com.dapr.event.sent",
        "topic": "x",
        "data": {"meta": meta.model_dump(), "amount": 4200},
    }
    payload = parse_cloudevent(_Payload, body)
    assert isinstance(payload, _Payload)
    assert payload.amount == 4200
    assert payload.meta.invoice_id == "inv-3"


def test_parse_cloudevent_missing_data_raises_value_error():
    with pytest.raises(ValueError, match="data"):
        parse_cloudevent(_Payload, {"specversion": "1.0", "topic": "x"})
