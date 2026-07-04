import uuid
from collections.abc import Callable
from typing import Any, Protocol

import httpx


class CasConflict(Exception):
    """Raised when a compare-and-swap loop exhausts its retries."""


class StateStore(Protocol):
    async def get(self, key: str) -> tuple[Any | None, str | None]: ...
    async def try_save(self, key: str, value: Any, etag: str | None) -> bool: ...
    async def delete(self, key: str) -> None: ...


class InMemoryStateStore:
    """Test fake with real etag semantics (stale writes rejected, None = first-write-only)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, str]] = {}

    async def get(self, key: str) -> tuple[Any | None, str | None]:
        if key not in self._data:
            return None, None
        value, etag = self._data[key]
        return value, etag

    async def try_save(self, key: str, value: Any, etag: str | None) -> bool:
        current = self._data.get(key)
        if etag is None and current is not None:
            return False  # first-write-only, but key exists
        if etag is not None and (current is None or current[1] != etag):
            return False  # stale or vanished
        self._data[key] = (value, uuid.uuid4().hex)
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class DaprStateStore:
    """StateStore over the Dapr sidecar HTTP API (v1.0/state)."""

    def __init__(self, store_name: str = "statestore", base_url: str = "http://localhost:3500"):
        self._url = f"{base_url}/v1.0/state/{store_name}"
        self._client = httpx.AsyncClient(timeout=10.0)

    async def get(self, key: str) -> tuple[Any | None, str | None]:
        resp = await self._client.get(f"{self._url}/{key}")
        if resp.status_code == 204 or not resp.content:
            return None, None
        resp.raise_for_status()
        return resp.json(), resp.headers.get("ETag")

    async def try_save(self, key: str, value: Any, etag: str | None) -> bool:
        entry: dict[str, Any] = {
            "key": key,
            "value": value,
            "options": {"concurrency": "first-write"},
        }
        if etag is not None:
            entry["etag"] = etag
        resp = await self._client.post(self._url, json=[entry])
        if resp.status_code == 409:
            return False  # standard etag-mismatch conflict
        if resp.status_code == 500:
            # Dapr's Redis state component runs both etag-CAS and
            # first-write-only checks through the same conditional-set Lua
            # script, and doesn't always surface a conflict as 409. Verified
            # against a real Redis-backed sidecar (Task 5 compose smoke,
            # scripts/smoke-compose.sh):
            #   - stale-etag mismatch: observed as 409 (handled above), but
            #     some deployments surface it as 500 with an "etag mismatch"
            #     style message in the body instead.
            #   - first-write-only (etag=None) against an existing key:
            #     observed as 500 with a body like "failed to set key ...
            #     script: ...", which does NOT mention "etag" at all.
            # Both are genuine CAS conflicts and must return False. Only treat
            # a 500 as a CAS conflict when the body actually says so;
            # otherwise a dead or misconfigured state store would silently
            # masquerade as a conflict instead of failing loudly (M15).
            body = resp.text.lower()
            if "etag" in body or "failed to set key" in body:
                return False
            resp.raise_for_status()
        resp.raise_for_status()
        return True

    async def delete(self, key: str) -> None:
        resp = await self._client.delete(f"{self._url}/{key}")
        resp.raise_for_status()


async def cas_update(
    store: StateStore,
    key: str,
    update_fn: Callable[[Any], Any],
    *,
    max_retries: int = 10,
) -> Any:
    for _ in range(max_retries):
        value, etag = await store.get(key)
        new_value = update_fn(value)
        if await store.try_save(key, new_value, etag):
            return new_value
    raise CasConflict(f"CAS on '{key}' failed after {max_retries} retries")


async def try_register(store: StateStore, key: str, value: Any) -> bool:
    """Atomic first-write-wins registration (dedupe fingerprints etc.)."""
    existing, _ = await store.get(key)
    if existing is not None:
        return False
    return await store.try_save(key, value, None)
