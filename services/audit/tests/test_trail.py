"""
Task 1: the append-only trail store (F9) + the ceiling-compliance index (F10).

F9 -- every event touching an invoice's approval lifecycle is appended to
`trail:{correlation_id}` via `cas_update`, so the CAS retry loop serializes
concurrent appends for the same correlation id instead of losing one under a
race (proven below with `YieldingStateStore` + `asyncio.gather`).

F10 -- a key-value store has no native "scan all auto-approvals and check
their ceiling" query, so `AuditTrail` maintains a second, queryable
projection: `index:auto-approvals`, a flat list of
`{correlation_id, invoice_id, usd_cents, ceiling_cents}` entries, one per
`decision-made` event with `route=auto_approve` (Task 2's subscriber appends
via `append_auto_approval`; this task only proves the read side).
`ceiling_violations()` reads that index and returns any entry whose
`usd_cents` exceeds its `ceiling_cents`. In a correct system this is ALWAYS
`[]` -- the empty list itself IS the F10 proof, not a placeholder for one.
The trail (`trail:{correlation_id}`) stays the source of truth; the index is
just a projection over it for the one query a KV store can't do natively.
"""

import asyncio

from afcommon.state import InMemoryStateStore, YieldingStateStore
from fastapi.testclient import TestClient

from services.audit.src import deps
from services.audit.src.main import app
from services.audit.src.trail import AuditTrail, TrailEntry


def make_entry(event_type: str, event_id: str, occurred_at: str, **payload) -> TrailEntry:
    return TrailEntry(
        event_type=event_type,
        event_id=event_id,
        occurred_at=occurred_at,
        payload=payload,
    )


async def test_append_then_get_trail_roundtrip():
    trail = AuditTrail(InMemoryStateStore())
    entry = make_entry("invoice-submitted", "evt-1", "2026-07-01T00:00:00Z", invoiceId="inv-1")
    await trail.append("corr-1", entry)

    got = await trail.get_trail("corr-1")
    assert got == [entry]


async def test_multiple_events_ordered_by_occurred_at():
    trail = AuditTrail(InMemoryStateStore())
    late = make_entry("decision-made", "evt-2", "2026-07-01T00:05:00Z")
    early = make_entry("invoice-submitted", "evt-1", "2026-07-01T00:00:00Z")

    # Appended out of order; get_trail must still return them sorted.
    await trail.append("corr-1", late)
    await trail.append("corr-1", early)

    got = await trail.get_trail("corr-1")
    assert [e.event_id for e in got] == ["evt-1", "evt-2"]


async def test_append_is_additive_second_append_keeps_first():
    trail = AuditTrail(InMemoryStateStore())
    first = make_entry("invoice-submitted", "evt-1", "2026-07-01T00:00:00Z")
    second = make_entry("decision-made", "evt-2", "2026-07-01T00:01:00Z")

    await trail.append("corr-1", first)
    await trail.append("corr-1", second)

    got = await trail.get_trail("corr-1")
    assert len(got) == 2
    assert {e.event_id for e in got} == {"evt-1", "evt-2"}


async def test_unknown_correlation_id_returns_empty_trail():
    trail = AuditTrail(InMemoryStateStore())
    got = await trail.get_trail("corr-does-not-exist")
    assert got == []


async def test_concurrent_appends_for_one_cid_both_retained():
    """INV-style race: two appends for the SAME correlation id, genuinely
    interleaved via YieldingStateStore + asyncio.gather. The CAS retry loop
    in `cas_update` must serialize them so neither is lost (F9's append-only
    guarantee would be broken by a lost append)."""
    trail = AuditTrail(YieldingStateStore())
    a = make_entry("invoice-submitted", "evt-a", "2026-07-01T00:00:00Z")
    b = make_entry("decision-made", "evt-b", "2026-07-01T00:01:00Z")

    await asyncio.gather(
        trail.append("corr-race", a),
        trail.append("corr-race", b),
    )

    got = await trail.get_trail("corr-race")
    assert {e.event_id for e in got} == {"evt-a", "evt-b"}


async def test_ceiling_violations_empty_when_all_in_ceiling():
    trail = AuditTrail(InMemoryStateStore())
    await trail.append_auto_approval(
        {"correlationId": "corr-1", "invoiceId": "inv-1", "usdCents": 100, "ceilingCents": 25000}
    )
    await trail.append_auto_approval(
        {"correlationId": "corr-2", "invoiceId": "inv-2", "usdCents": 25000, "ceilingCents": 25000}
    )

    assert await trail.ceiling_violations() == []


async def test_ceiling_violations_returns_the_bad_synthetic_entry():
    trail = AuditTrail(InMemoryStateStore())
    ok_entry = {
        "correlationId": "corr-1", "invoiceId": "inv-1",
        "usdCents": 100, "ceilingCents": 25000,
    }
    bad_entry = {
        "correlationId": "corr-2", "invoiceId": "inv-2",
        "usdCents": 30000, "ceilingCents": 25000,
    }
    await trail.append_auto_approval(ok_entry)
    await trail.append_auto_approval(bad_entry)

    violations = await trail.ceiling_violations()
    assert violations == [bad_entry]


async def test_auto_approval_count_reflects_index_size():
    trail = AuditTrail(InMemoryStateStore())
    assert await trail.auto_approval_count() == 0
    await trail.append_auto_approval(
        {"correlationId": "corr-1", "invoiceId": "inv-1", "usdCents": 1, "ceilingCents": 2}
    )
    await trail.append_auto_approval(
        {"correlationId": "corr-2", "invoiceId": "inv-2", "usdCents": 1, "ceilingCents": 2}
    )
    assert await trail.auto_approval_count() == 2


# --- API tests (DI-overridden AuditTrail over InMemoryStateStore) ---------


def make_client(trail: AuditTrail) -> TestClient:
    app.dependency_overrides[deps.get_trail] = lambda: trail
    return TestClient(app)


def teardown_function(_fn):
    app.dependency_overrides.clear()


def test_get_trail_endpoint_unknown_cid_returns_200_empty():
    trail = AuditTrail(InMemoryStateStore())
    client = make_client(trail)

    resp = client.get("/trail/corr-unknown")
    assert resp.status_code == 200
    assert resp.json() == {"correlationId": "corr-unknown", "entries": []}


async def test_get_trail_endpoint_returns_sorted_entries():
    trail = AuditTrail(InMemoryStateStore())
    late = make_entry("decision-made", "evt-2", "2026-07-01T00:05:00Z")
    early = make_entry("invoice-submitted", "evt-1", "2026-07-01T00:00:00Z")
    await trail.append("corr-1", late)
    await trail.append("corr-1", early)

    client = make_client(trail)
    resp = client.get("/trail/corr-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["correlationId"] == "corr-1"
    assert [e["eventId"] if "eventId" in e else e["event_id"] for e in body["entries"]] == [
        "evt-1",
        "evt-2",
    ]


async def test_ceiling_compliance_endpoint_empty_violations_is_the_f10_proof():
    trail = AuditTrail(InMemoryStateStore())
    await trail.append_auto_approval(
        {"correlationId": "corr-1", "invoiceId": "inv-1", "usdCents": 100, "ceilingCents": 25000}
    )
    client = make_client(trail)

    resp = client.get("/audit/ceiling-compliance")
    assert resp.status_code == 200
    assert resp.json() == {"autoApprovalsChecked": 1, "violations": []}


async def test_ceiling_compliance_endpoint_surfaces_a_violation():
    trail = AuditTrail(InMemoryStateStore())
    bad_entry = {
        "correlationId": "corr-2", "invoiceId": "inv-2",
        "usdCents": 30000, "ceilingCents": 25000,
    }
    await trail.append_auto_approval(bad_entry)
    client = make_client(trail)

    resp = client.get("/audit/ceiling-compliance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["autoApprovalsChecked"] == 1
    assert body["violations"] == [bad_entry]
