from afcommon.logging import correlation_id_var
from afcommon.middleware import CorrelationIdMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def get_correlation_id(request):
    """Route that returns the current correlation_id in the context."""
    cid = correlation_id_var.get()
    return JSONResponse({"correlation_id": cid})


def create_test_app():
    """Create a minimal Starlette app with CorrelationIdMiddleware."""
    routes = [
        Route("/", get_correlation_id),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(CorrelationIdMiddleware)
    return app


def test_incoming_correlation_id_is_bound_and_echoed():
    """Send X-Correlation-Id header; assert it's bound in contextvar and echoed in response."""
    app = create_test_app()
    client = TestClient(app)

    response = client.get("/", headers={"X-Correlation-Id": "corr-in-1"})

    # Check that the route saw the correlation_id in the contextvar
    assert response.json()["correlation_id"] == "corr-in-1"
    # Check that the response echoes the same correlation_id header
    assert response.headers["X-Correlation-Id"] == "corr-in-1"


def test_missing_correlation_id_is_minted():
    """Send no X-Correlation-Id header; assert a value starting with 'corr-' is minted."""
    app = create_test_app()
    client = TestClient(app)

    response = client.get("/")

    # Check that the route saw a correlation_id starting with "corr-"
    minted_id = response.json()["correlation_id"]
    assert minted_id.startswith("corr-")
    # Check that the response echoes the same minted correlation_id header
    assert response.headers["X-Correlation-Id"] == minted_id


def test_context_is_reset_after_request():
    """After a request completes, assert correlation_id_var is reset to default."""
    app = create_test_app()
    client = TestClient(app)

    # Before any request, correlation_id_var should be at default
    assert correlation_id_var.get() == "-"

    # Make a request with a correlation_id
    client.get("/", headers={"X-Correlation-Id": "corr-test-reset"})

    # After the request completes, the context should be reset to default
    assert correlation_id_var.get() == "-"
