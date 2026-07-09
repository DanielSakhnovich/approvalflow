import asyncio
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
            # Dapr's Redis state component wraps EVERY error from its
            # conditional-set Lua script as `failed to set key %s: %w`
            # (components-contrib v1.15.4, state/redis/redis.go) - including
            # genuine infra failures (OOM, connection reset, timeouts), not
            # just CAS conflicts. A bare substring match on "failed to set
            # key" would silently swallow those as "conflict" instead of
            # raising (M15). Gate each branch on the etag mode that can
            # actually produce it, and additionally require the Lua
            # conditional-check's own signature for the first-write branch,
            # since that's the only part of the wrapped body that's specific
            # to a real conflict rather than the generic wrapper text:
            #   - etag present (CAS mismatch): observed as 409 (handled
            #     above), but some deployments surface it as 500 with an
            #     "etag mismatch" style message in the body instead. NOTE:
            #     this substring check is a residual risk symmetric to the
            #     one below - Dapr wraps ANY etag-mode Redis error as
            #     ETagMismatch-flavored text too, so a real infra failure in
            #     etag mode could in principle also be misread as a
            #     conflict; this is an upstream Dapr limitation, not
            #     something afcommon can fully close from the client side.
            #   - first-write-only (etag=None) against an existing key:
            #     verified against a real Redis-backed sidecar (Task 5/6
            #     compose smoke, scripts/smoke-compose.sh) to come back as
            #     500 with a body containing BOTH "failed to set key" (the
            #     wrapper) AND "user_script" (the Lua conditional-check
            #     script's own error signature, e.g. "ERR user_script:14:
            #     failed to set key ... script: <hash>, on @user_script:14.").
            #     Requiring both means a bare wrapper body with no Lua
            #     signature (e.g. "failed to set key app||k: connection
            #     reset by peer") falls through and raises instead of being
            #     treated as a conflict.
            #   - Postgres component (state.postgresql, the audit store per
            #     D-017): EMPIRICALLY characterized against Dapr 1.15.4 in the
            #     Phase 06 compose smoke (transcript prints the raw body).
            #     Observed: stale-etag -> 409 (handled above); first-write-only
            #     against an existing key -> 500 with "no item was updated" (the
            #     conditional write affected zero rows). "no item was updated"
            #     is the post-query row-count check, which only runs after the
            #     query executes, so a connection/infra failure surfaces as
            #     different driver text instead -- it's a safe positive signal.
            #     Caveat (dapr/components-contrib#2773): some versions can also
            #     emit "no item was updated" for a real ETAG mismatch instead of
            #     a clean 409. 1.15.4 does not, but to stay correct across a
            #     component upgrade we accept that phrase in BOTH etag modes:
            #     either way it means the conditional write was rejected, which
            #     is exactly what try_save's False return signals. Re-verify the
            #     bodies (the smoke prints them) on any Dapr/component bump.
            body = resp.text.lower()
            if etag is not None and ("etag" in body or "no item was updated" in body):
                return False
            if etag is None and (
                ("failed to set key" in body and "user_script" in body)  # Redis
                or "no item was updated" in body  # Postgres
            ):
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


class YieldingStateStore(InMemoryStateStore):
    """InMemoryStateStore that actually suspends on get/try_save.

    Plain InMemoryStateStore never awaits anything, so under asyncio.gather
    concurrent tasks would just run to completion one after another -
    proving nothing about interleaving. Inserting a real suspension point
    (await asyncio.sleep(0)) before delegating lets the event loop switch
    between tasks mid-CAS-loop, so concurrent CAS loops can genuinely
    race for the same key. Test utility, like InMemoryStateStore above.
    """

    def __init__(self) -> None:
        super().__init__()
        self.conflicts = 0

    async def get(self, key: str):
        await asyncio.sleep(0)
        return await super().get(key)

    async def try_save(self, key: str, value, etag) -> bool:
        await asyncio.sleep(0)
        ok = await super().try_save(key, value, etag)
        if not ok:
            self.conflicts += 1
        return ok
