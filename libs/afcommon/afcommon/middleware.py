import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .logging import correlation_id_var

CORRELATION_HEADER = "X-Correlation-Id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Reads X-Correlation-Id (or mints one) and binds it to the logging context."""

    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get(CORRELATION_HEADER) or f"corr-{uuid.uuid4()}"
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers[CORRELATION_HEADER] = cid
        return response
