from afcommon.events import (
    TOPIC_INVOICE_SUBMITTED,
    EventMeta,
    new_event_meta,
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
