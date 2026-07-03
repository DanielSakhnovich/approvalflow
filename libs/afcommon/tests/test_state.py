import asyncio

import pytest
from afcommon.state import CasConflict, InMemoryStateStore, cas_update, try_register


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


async def test_cas_update_retries_past_conflicts():
    store = InMemoryStateStore()
    await store.try_save("counter", 0, None)

    async def bump_100_times():
        for _ in range(100):
            await cas_update(store, "counter", lambda v: (v or 0) + 1, max_retries=200)

    await asyncio.gather(bump_100_times(), bump_100_times())
    value, _ = await store.get("counter")
    assert value == 200  # no lost updates under concurrency


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
