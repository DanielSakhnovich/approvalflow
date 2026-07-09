"""Task 3: GET /invoices/{id}?trail=true composes the audit trail via a Dapr
service-invocation call to audit-svc (M5's synchronous leg). Without the flag,
behavior is byte-identical to before and audit is NOT called. A failing audit
call degrades gracefully to an empty trail -- the status view always renders.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from services.intake.src import deps
from services.intake.src.audit_client import AuditInvokeClient
from services.intake.src.main import app
from services.intake.tests.test_models import FIXTURE


class FakeAudit:
    def __init__(self, entries=None, raises=False):
        self.entries = entries or []
        self.raises = raises
        self.calls = 0

    async def fetch_trail(self, correlation_id: str) -> list[dict]:
        self.calls += 1
        if self.raises:
            raise RuntimeError("audit down")
        return self.entries


@pytest.fixture
def env():
    from afcommon.state import InMemoryStateStore

    from services.intake.src.repo import IntakeRepo
    repo = IntakeRepo(InMemoryStateStore())

    async def fake_publish(topic, payload):
        pass

    app.dependency_overrides[deps.get_repo] = lambda: repo
    app.dependency_overrides[deps.get_publisher] = lambda: fake_publish
    yield TestClient(app), repo
    app.dependency_overrides.clear()


def _submit(client) -> str:
    return client.post("/api/invoices", json=FIXTURE).json()["trackingId"]


def test_trail_true_includes_audit_entries(env):
    client, _ = env
    fake = FakeAudit(entries=[{"event_type": "invoice-submitted"},
                              {"event_type": "decision-made"}])
    app.dependency_overrides[deps.get_audit_client] = lambda: fake
    tracking = _submit(client)

    resp = client.get(f"/api/invoices/{tracking}?trail=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trackingId"] == tracking  # normal fields intact
    assert [e["event_type"] for e in body["trail"]] == ["invoice-submitted", "decision-made"]
    assert fake.calls == 1


def test_no_flag_omits_trail_and_never_calls_audit(env):
    client, _ = env
    fake = FakeAudit(entries=[{"event_type": "x"}])
    app.dependency_overrides[deps.get_audit_client] = lambda: fake
    tracking = _submit(client)

    resp = client.get(f"/api/invoices/{tracking}")
    assert resp.status_code == 200
    assert "trail" not in resp.json()
    assert fake.calls == 0  # audit not invoked without the flag


def test_trail_true_degrades_gracefully_when_audit_down(env):
    client, _ = env
    app.dependency_overrides[deps.get_audit_client] = lambda: FakeAudit(raises=True)
    tracking = _submit(client)

    resp = client.get(f"/api/invoices/{tracking}?trail=true")
    assert resp.status_code == 200  # status still renders
    assert resp.json()["trail"] == []  # empty, not an error


async def test_real_client_builds_dapr_invoke_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"correlationId": "corr-1",
                                         "entries": [{"event_type": "decision-made"}]})

    transport = httpx.MockTransport(handler)

    class PatchedClient(AuditInvokeClient):
        async def fetch_trail(self, correlation_id: str) -> list[dict]:
            url = (f"{self._base_url}/v1.0/invoke/audit-svc/method/"
                   f"trail/{correlation_id}")
            async with httpx.AsyncClient(transport=transport) as client:
                resp = await client.get(url)
                return resp.json().get("entries", [])

    entries = await PatchedClient().fetch_trail("corr-1")
    assert seen["url"].endswith("/v1.0/invoke/audit-svc/method/trail/corr-1")
    assert entries == [{"event_type": "decision-made"}]
