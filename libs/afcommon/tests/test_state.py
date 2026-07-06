import asyncio

import httpx
import pytest
from afcommon.state import (
    CasConflict,
    DaprStateStore,
    InMemoryStateStore,
    YieldingStateStore,
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


async def test_dapr_try_save_500_first_write_conflict_body_is_conflict():
    # Real body captured verbatim against a live Dapr Redis sidecar (Task 5/6
    # compose smoke, scripts/smoke-compose.sh): a first-write-only save
    # (etag=None) against an existing key comes back as 500 with a body that
    # contains both the generic wrapper ("failed to set key") AND the Lua
    # conditional-check script's own error signature ("user_script").
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "errorCode": "ERR_STATE_SAVE",
                "message": "failed saving state in state store statestore: "
                "failed to set key intake-api||validate-conflict-body: ERR "
                "user_script:14: failed to set key "
                "intake-api||validate-conflict-body script: "
                "d908b9553add63a82e03589b5c6d01a7654ef0f2, on @user_script:14.",
            },
        )

    store = _dapr_store_with_transport(handler)
    assert not await store.try_save("k", {"a": 1}, None)


async def test_dapr_try_save_500_first_write_infra_error_raises():
    # A real infra failure (e.g. Redis OOM, connection reset) wrapped by
    # Dapr's Redis component as "failed to set key %s: %w" but WITHOUT the
    # Lua conditional-check's own "user_script" signature must not be
    # mistaken for a first-write conflict - it must raise instead of
    # silently reporting "conflict".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "errorCode": "ERR_STATE_SAVE",
                "message": "failed to set key app||k: connection reset by peer",
            },
        )

    store = _dapr_store_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await store.try_save("k", {"a": 1}, None)


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


async def test_dapr_get_204_empty_body_returns_none_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    store = _dapr_store_with_transport(handler)
    value, etag = await store.get("missing-key")
    assert value is None
    assert etag is None


async def test_dapr_get_200_extracts_value_and_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"a": 1}, headers={"ETag": "etag-123"})

    store = _dapr_store_with_transport(handler)
    value, etag = await store.get("k")
    assert value == {"a": 1}
    assert etag == "etag-123"


async def test_dapr_delete_204_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    store = _dapr_store_with_transport(handler)
    assert await store.delete("k") is None


async def test_dapr_delete_500_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errorCode": "ERR_STATE_DELETE", "message": "boom"})

    store = _dapr_store_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await store.delete("k")
