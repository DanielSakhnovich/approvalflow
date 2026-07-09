"""M5 synchronous service-to-service call: intake fetches an invoice's audit
trail from audit-svc via Dapr service invocation.

This is the system's ONE sync service-to-service call -- everything else flows
through pub/sub. It's deliberately best-effort: the audit trail enriches the
status view (F9) but a status response must still render if audit is down, so
a failure returns [] and logs a warning rather than propagating. The trail is
supplementary detail, not the primary answer.
"""

import logging

import httpx

log = logging.getLogger(__name__)

_DAPR_BASE = "http://localhost:3500"


class AuditInvokeClient:
    def __init__(self, base_url: str = _DAPR_BASE):
        self._base_url = base_url

    async def fetch_trail(self, correlation_id: str) -> list[dict]:
        url = (f"{self._base_url}/v1.0/invoke/audit-svc/method/"
               f"trail/{correlation_id}")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json().get("entries", [])
        except (httpx.HTTPError, ValueError):
            log.warning("audit trail fetch failed for correlation_id=%s; "
                        "status view rendered without trail", correlation_id)
            return []
