"""
The D5/B1 seed and the phase's acceptance test: every one of the 20 shipped
fixtures (`sample-invoices.json`), submitted through the REAL wired
`DecisionPipeline` (real Gate 1 fingerprint registry, real Gate 2 validate(),
real config-seeded thresholds/FX, real router, the honest `StubAgent` — not
the malicious one M12 uses to prove the ceiling can't be gamed) must come out
with `route == expected.route`, in file order (so INV-1001 is processed
before INV-1007, its duplicate).

Where M12 (test_m12_adversarial.py) proves the router can't be tricked by a
malicious agent, this proves the whole assembled pipeline agrees with the
fixture author's own ground truth using a normal, honest agent — the
stub-vs-fixture drift check called out as the phase's #1 risk.
"""

import json
from pathlib import Path

from afcommon.contracts import InvoiceSubmittedPayload
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore

from services.decision.src.agents.stub import StubAgent
from services.decision.src.config import ConfigRepo
from services.decision.src.fingerprint import FingerprintRegistry
from services.decision.src.pipeline import DecisionPipeline
from services.decision.src.trust import TrustRepo


def _fixtures() -> list[dict]:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "sample-invoices.json").exists():
            return json.loads((parent / "sample-invoices.json").read_text())["fixtures"]
    raise FileNotFoundError("sample-invoices.json not found")


FIXTURES = _fixtures()


class _NullPublisher:
    async def __call__(self, topic: str, payload: dict) -> None:
        pass


async def _run_all_fixtures() -> dict[str, str]:
    """Submit every shipped fixture through the real wired pipeline, in file
    order, and return {fixture_id: actual_route}."""
    store = InMemoryStateStore()
    pipeline = DecisionPipeline(
        config=ConfigRepo(store),
        fingerprints=FingerprintRegistry(store),
        trust=TrustRepo(store),
        agent=StubAgent(),
        publisher=_NullPublisher(),
        policy_rules="",  # StubAgent ignores policy_rules by design
    )
    routes: dict[str, str] = {}
    for fixture in FIXTURES:  # file order: INV-1001 registered before INV-1007
        payload = InvoiceSubmittedPayload(
            meta=new_event_meta(fixture["id"], f"corr-{fixture['id']}"), invoice=fixture
        )
        result = await pipeline.handle_submission(payload)
        routes[fixture["id"]] = result.route
    return routes


async def test_every_shipped_fixture_routes_as_expected():
    routes = await _run_all_fixtures()
    mismatches = [
        (f["id"], f["expected"]["route"], routes[f["id"]])
        for f in FIXTURES
        if routes[f["id"]] != f["expected"]["route"]
    ]
    assert mismatches == [], (
        "fixture(s) routed differently than labeled (id, expected, actual): " f"{mismatches}"
    )


async def test_all_twenty_fixtures_were_exercised():
    routes = await _run_all_fixtures()
    assert len(routes) == 20
    assert len(FIXTURES) == 20


async def test_inv1007_is_the_duplicate_of_inv1001():
    routes = await _run_all_fixtures()
    assert routes["INV-1001"] == "auto_approve"
    assert routes["INV-1007"] == "duplicate"


async def test_exactly_four_fixtures_auto_approve():
    """F6: the posture keeps autonomy meaningful -- not zero, not everything."""
    routes = await _run_all_fixtures()
    auto = [fid for fid, route in routes.items() if route == "auto_approve"]
    assert sorted(auto) == ["INV-1001", "INV-1002", "INV-1016", "INV-1017"]
