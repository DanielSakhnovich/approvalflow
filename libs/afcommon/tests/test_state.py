import asyncio

import httpx
import pytest
from afcommon.state import (
    CasConflict,
    DaprStateStore,
    InMemoryStateStore,
    cas_update,
    try_register,
)


async def test_get_missing_key_returns_none():
    store = InMemoryStateStore()
    value, etag = await store.get("nope")
    assert value is None and etag is None


async def test_save_and_get_roundtrip():
    store = InMemoryStateStore()
    assert await store.try_save("k", {"a": 1}, None)
    value, etag = await store.get("k")
    assert value == {"a": 1} and etag is not None


async def test_stale_etag_write_is_rejected():
    store = InMemoryStateStore()
    await store.try_save("k", 1, None)
    _, etag = await store.get("k")
    assert await store.try_save("k", 2, etag)          # fresh etag wins
    assert not await store.try_save("k", 3, etag)      # stale etag loses


async def test_first_write_only_rejected_when_key_exists():
    store = InMemoryStateStore()
    assert await store.try_save("k", 1, None)
    assert not await store.try_save("k", 2, None)      # None etag = first-write-only


class YieldingStateStore(InMemoryStateStore):
    """InMemoryStateStore that actually suspends on get/try_save.

    Plain InMemoryStateStore never awaits anything, so under asyncio.gather
    the two tasks below would just run to completion one after another -
    proving nothing about interleaving. Inserting a real suspension point
    (await asyncio.sleep(0)) before delegating lets the event loop switch
    between tasks mid-CAS-loop, so two concurrent bumpers can genuinely
    race for the same key.
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


async def test_cas_update_retries_past_conflicts():
    store = YieldingStateStore()
    await store.try_save("counter", 0, None)

    async def bump_100_times():
        for _ in range(100):
            await cas_update(store, "counter", lambda v: (v or 0) + 1, max_retries=200)

    await asyncio.gather(bump_100_times(), bump_100_times())
    value, _ = await store.get("counter")
    # Proves both that no updates were lost under real interleaving (value
    # == 200) and that contention actually occurred and was survived
    # (conflicts > 0) - not just that the two tasks ran back-to-back.
    assert value == 200
    assert store.conflicts > 0


async def test_cas_update_raises_after_max_retries():
    store = InMemoryStateStore()
    await store.try_save("k", 0, None)

    class AlwaysConflict(InMemoryStateStore):
        async def try_save(self, key, value, etag):
            return False

    bad = AlwaysConflict()
    await bad.__class__.__mro__[1].try_save(bad, "k", 0, None)  # seed via parent
    with pytest.raises(CasConflict):
        await cas_update(bad, "k", lambda v: v, max_retries=3)


async def test_try_register_is_first_write_wins():
    store = InMemoryStateStore()
    assert await try_register(store, "fp:abc", {"invoiceId": "INV-1"})
    assert not await try_register(store, "fp:abc", {"invoiceId": "INV-2"})
    value, _ = await store.get("fp:abc")
    assert value == {"invoiceId": "INV-1"}


def _dapr_store_with_transport(handler) -> DaprStateStore:
    store = DaprStateStore()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return store


async def test_dapr_try_save_409_is_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"errorCode": "ERR_STATE_SAVE", "message": "etag mismatch"})

    store = _dapr_store_with_transport(handler)
    assert not await store.try_save("k", {"a": 1}, "some-etag")


async def test_dapr_try_save_500_etag_mismatch_body_is_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"errorCode": "ERR_STATE_SAVE", "message": "possible etag mismatch"},
        )

    store = _dapr_store_with_transport(handler)
    assert not await store.try_save("k", {"a": 1}, "some-etag")


async def test_dapr_try_save_500_other_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "errorCode": "ERR_STATE_STORE_NOT_FOUND",
                "message": "state store statestore is not found",
            },
        )

    store = _dapr_store_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await store.try_save("k", {"a": 1}, "some-etag")
