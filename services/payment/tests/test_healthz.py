from fastapi.testclient import TestClient

from services.payment.src.main import app


def test_healthz_ok():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "payment-svc"}


def test_healthz_echoes_correlation_id():
    client = TestClient(app)
    resp = client.get("/healthz", headers={"X-Correlation-Id": "corr-test-1"})
    assert resp.headers["X-Correlation-Id"] == "corr-test-1"
