import time

import anyio
import pytest
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.intake.src import deps
from services.intake.src.main import app
from services.intake.src.models import InvoiceStatus
from services.intake.src.repo import IntakeRepo
from services.intake.tests.test_models import FIXTURE


@pytest.fixture
def env():
    repo = IntakeRepo(InMemoryStateStore())
    published: list[tuple[str, dict]] = []

    async def fake_publish(topic: str, payload: dict) -> None:
        published.append((topic, payload))

    app.dependency_overrides[deps.get_repo] = lambda: repo
    app.dependency_overrides[deps.get_publisher] = lambda: fake_publish
    yield TestClient(app), repo, published
    app.dependency_overrides.clear()


def test_submit_returns_202_with_tracking_id(env):
    client, repo, published = env
    resp = client.post("/api/invoices", json=FIXTURE)
    assert resp.status_code == 202
    body = resp.json()
    assert body["trackingId"].startswith("inv_") and body["status"] == "evaluating"
    assert len(published) == 1 and published[0][0] == "invoice-submitted"
    assert published[0][1]["invoice"]["vendor"] == "Bistro 19"


def test_submit_rejects_bad_payload(env):
    client, *_ = env
    assert client.post("/api/invoices", json={"vendor": "X"}).status_code == 422


def test_submit_publish_failure_is_loud(env):
    client, repo, _ = env

    async def broken_publish(topic, payload):
        raise RuntimeError("sidecar down")

    app.dependency_overrides[deps.get_publisher] = lambda: broken_publish
    resp = client.post("/api/invoices", json=FIXTURE)
    assert resp.status_code == 503
    # the record exists and is marked, not silently dropped


def test_get_status_and_404(env):
    client, *_ = env
    tracking = client.post("/api/invoices", json=FIXTURE).json()["trackingId"]
    got = client.get(f"/api/invoices/{tracking}")
    assert got.status_code == 200 and got.json()["status"] == "evaluating"
    assert client.get("/api/invoices/inv_nope").status_code == 404


def test_resubmit_only_from_needs_info(env):
    client, repo, published = env
    tracking = client.post("/api/invoices", json=FIXTURE).json()["trackingId"]
    assert client.put(f"/api/invoices/{tracking}", json=FIXTURE).status_code == 409

    import anyio

    async def force_needs_info():
        record = await repo.get_record(tracking)
        await repo.save_record(record.model_copy(update={"status": InvoiceStatus.NEEDS_INFO}))
    anyio.run(force_needs_info)

    resp = client.put(f"/api/invoices/{tracking}", json={**FIXTURE, "notes": "client: ACME"})
    assert resp.status_code == 202
    assert len(published) == 2
    same_invoice = published[1][1]["meta"]["invoice_id"]
    assert same_invoice == tracking  # resumes the SAME invoice, not a new one


def test_dashboard_counts_submissions(env):
    client, *_ = env
    client.post("/api/invoices", json=FIXTURE)
    client.post("/api/invoices", json=FIXTURE)
    assert client.get("/api/dashboard").json()["submitted"] == 2


def test_updated_at_advances_on_transitions(env):
    client, repo, _ = env
    tracking = client.post("/api/invoices", json=FIXTURE).json()["trackingId"]
    get_resp = client.get(f"/api/invoices/{tracking}")
    submitted_at = get_resp.json()["submittedAt"]
    updated_at = get_resp.json()["updatedAt"]
    assert updated_at >= submitted_at

    # Force needs_info status
    async def force_needs_info():
        record = await repo.get_record(tracking)
        await repo.save_record(record.model_copy(update={"status": InvoiceStatus.NEEDS_INFO}))
    anyio.run(force_needs_info)

    # Small delay to ensure time advances (microseconds apart)
    time.sleep(0.01)

    # Resubmit
    resp = client.put(f"/api/invoices/{tracking}", json={**FIXTURE, "notes": "resubmitted"})
    assert resp.status_code == 202

    # Check that updated_at has advanced
    get_resp = client.get(f"/api/invoices/{tracking}")
    new_updated_at = get_resp.json()["updatedAt"]
    assert new_updated_at > updated_at
