import pytest
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.decision.src import deps
from services.decision.src.config import ConfigRepo
from services.decision.src.main import app


async def test_thresholds_seed_defaults():
    repo = ConfigRepo(InMemoryStateStore())
    t = await repo.get_thresholds()
    assert t.ceiling_cents == 25000 and t.min_confidence == 0.80
    assert t.trusted_ceiling_cents == 40000


async def test_update_thresholds_merges_and_persists():
    repo = ConfigRepo(InMemoryStateStore())
    updated = await repo.update_thresholds({"ceiling_cents": 30000})
    assert updated.ceiling_cents == 30000 and updated.min_confidence == 0.80
    again = await repo.get_thresholds()
    assert again.ceiling_cents == 30000


async def test_update_rejects_unknown_keys():
    repo = ConfigRepo(InMemoryStateStore())
    with pytest.raises(ValueError):
        await repo.update_thresholds({"banana": 1})


async def test_fx_rates_seeded():
    repo = ConfigRepo(InMemoryStateStore())
    rates = await repo.get_fx_rates()
    assert rates["EUR"] == 1.08 and rates["GBP"] == 1.27


@pytest.fixture
def client():
    repo = ConfigRepo(InMemoryStateStore())
    app.dependency_overrides[deps.get_config_repo] = lambda: repo
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_config_endpoint_roundtrip(client):
    assert client.get("/api/config/thresholds").json()["ceiling_cents"] == 25000
    resp = client.put("/api/config/thresholds", json={"min_confidence": 0.9})
    assert resp.status_code == 200 and resp.json()["min_confidence"] == 0.9
    assert client.put("/api/config/thresholds", json={"nope": 1}).status_code == 422
