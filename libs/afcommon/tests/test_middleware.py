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


async def test_context_is_reset_after_request():
    """After the middleware completes, correlation_id_var must be reset to default
    in the SAME task/context that made the call.

    This drives CorrelationIdMiddleware directly as ASGI (no TestClient, no thread
    hop) so the assertion runs in the same context that the middleware's
    `finally: reset(token)` executed in. Starlette's TestClient runs the app on a
    separate anyio portal thread, so a ContextVar assertion made on the test's
    MainThread after client.get() passes trivially regardless of whether the
    middleware actually resets the var -- that version of this test was vacuous.
    """

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = CorrelationIdMiddleware(inner_app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-correlation-id", b"corr-reset-check")],
        "query_string": b"",
    }
    received = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        received.append(message)

    # Before the call, correlation_id_var should be at default in this context.
    assert correlation_id_var.get() == "-"

    await app(scope, receive, send)

    # After the middleware completes, the reset must have restored the default
    # in THIS context -- the same context the middleware itself ran in.
    assert correlation_id_var.get() == "-"
