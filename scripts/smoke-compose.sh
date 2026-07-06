#!/usr/bin/env bash
# Smoke-test the compose skeleton: redis + dapr placement + intake-api + its
# daprd sidecar, then characterize real Dapr Redis state-store ETag/CAS
# semantics against libs/afcommon/afcommon/state.py's DaprStateStore.
#
# Run from repo root:
#   ./scripts/smoke-compose.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f dapr/secrets.json ]; then
  cp dapr/secrets.example.json dapr/secrets.json
fi

echo "=== docker compose up --build -d ==="
docker compose up --build -d

echo "=== waiting for intake-api to become healthy ==="
for i in $(seq 1 30); do
  status="$(docker compose ps intake-api --format '{{.Health}}' 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    echo "intake-api is healthy (after ${i}x2s)"
    break
  fi
  sleep 2
done

echo "=== curl http://localhost:8001/healthz ==="
curl -sf http://localhost:8001/healthz
echo

echo "=== intake-api-dapr placement log lines ==="
docker compose logs intake-api-dapr | grep -i "placement" | head -3

echo "=== Dapr state-store smoke + ETag characterization (exec'd inside intake-api) ==="
docker compose exec -T intake-api python -c "
import asyncio, json, sys
sys.path.insert(0, '/opt/afcommon')
import httpx
from afcommon.state import DaprStateStore

BASE = 'http://localhost:3500/v1.0/state/statestore'

async def raw_save(key, value, etag=None):
    entry = {'key': key, 'value': value, 'options': {'concurrency': 'first-write'}}
    if etag is not None:
        entry['etag'] = etag
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(BASE, json=[entry])
        return resp.status_code, resp.text

async def raw_get(key):
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(f'{BASE}/{key}')
        return resp.status_code, resp.json() if resp.content else None, resp.headers.get('ETag')

async def main():
    # --- Baseline smoke from the brief: first-write via try_save, then get roundtrip ---
    s = DaprStateStore()
    assert await s.try_save('smoke', {'ok': True}, None)
    v, etag = await s.get('smoke')
    assert v == {'ok': True} and etag, (v, etag)
    print('DAPR STATE SMOKE: OK')

    # --- Extended ETag characterization ---
    key = 'smoke-etag-char'
    # 1a. First save (no etag) on a brand-new key.
    status1, body1 = await raw_save(key, {'n': 1})
    print(f'CHAR-1a first-save status={status1} body={body1!r}')

    # 1b. Read back etag.
    _, val, etag1 = await raw_get(key)
    print(f'CHAR-1b read-back value={val} etag={etag1!r}')
    assert etag1, 'expected an etag after first save'

    # 1c. Save again WITH the current (fresh) etag -- should succeed.
    status2, body2 = await raw_save(key, {'n': 2}, etag1)
    print(f'CHAR-1c fresh-etag-save status={status2} body={body2!r}')
    assert status2 in (200, 204), f'expected fresh-etag save to succeed, got {status2}: {body2}'

    # 1d. Attempt a save with the now-STALE etag (etag1 is stale after 1c).
    status3, body3 = await raw_save(key, {'n': 3}, etag1)
    print(f'CHAR-1d stale-etag-save status={status3} body={body3!r}')

    if status3 == 409:
        print('STALE-ETAG: 409 Conflict -> try_save returns False (correct)')
    elif status3 == 500 and 'etag' in body3.lower():
        print(f'STALE-ETAG: 500 body contains \'etag\' ({body3!r}) -> try_save returns False (correct)')
    else:
        print(f'STALE-ETAG: UNEXPECTED status={status3} body={body3!r} -- does not match either known conflict shape')

    # Confirm DaprStateStore.try_save's boolean interpretation agrees with the raw observation.
    ok = await s.try_save(key, {'n': 99}, etag1)
    print(f'CHAR-1e try_save(stale etag) -> {ok} (expected False)')
    assert ok is False, 'try_save should reject a stale etag'

    # 2. First-write-only (etag=None) save attempted against an EXISTING key.
    status4, body4 = await raw_save(key, {'n': 4})  # no etag => first-write concurrency
    print(f'CHAR-2a first-write-only-on-existing-key status={status4} body={body4!r}')

    fw_ok = await s.try_save(key, {'n': 100}, None)
    print(f'CHAR-2b try_save(existing key, etag=None) -> {fw_ok} (expected False)')

    if fw_ok is False:
        print('FIRST-WRITE-ONLY: rejected on existing key -> try_save returns False (correct)')
    else:
        print('FIRST-WRITE-ONLY: UNEXPECTED -- try_save accepted a first-write-only save against an existing key')
        sys.exit(1)

    assert fw_ok is False, 'try_save(etag=None) must reject writes to an existing key'

    print('ETAG CHARACTERIZATION: DONE')

asyncio.run(main())
"

echo "--- intake E2E ---"
PAYLOAD='{"id":"INV-1001","submitter":"dana.cohen@northwind.example","department":"engineering-2026Q2","vendor":"Bistro 19","vendorKnown":true,"invoiceNumber":"NW-INV-7781","currency":"USD","category":"meals","attendees":1,"lineItems":[{"description":"Team lunch","quantity":1,"unitPrice":38.89}],"taxAmount":3.11,"total":42.0,"receiptPresent":true,"date":"2026-05-12","notes":"smoke"}'
TRACKING=$(curl -sf -X POST http://localhost:8001/api/invoices \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")
echo "tracking: $TRACKING"

STATUS=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
[ "$STATUS" = "evaluating" ] || { echo "FAIL: expected evaluating, got $STATUS"; exit 1; }

# loopback proof: intake's own subscription received the event it published
# (poll dapr subscription delivery via the dedupe key appearing in redis; a
# fixed single sleep is flaky now that decision-svc's sidecar is also
# contending for startup resources -- delivery still lands, just not always
# inside a fixed 2s window, so poll instead of widening a static sleep)
FOUND=""
for i in $(seq 1 15); do
  if docker compose exec -T redis redis-cli --scan --pattern 'intake-api||processed:*' | grep -q processed; then
    FOUND=1
    break
  fi
  sleep 1
done
[ -n "$FOUND" ] || { echo "FAIL: no processed-event key — pub/sub loopback did not deliver"; exit 1; }

curl -sf http://localhost:8001/api/dashboard | grep -q '"submitted"' \
  || { echo "FAIL: dashboard missing submitted counter"; exit 1; }
echo "INTAKE E2E: OK"

echo "=== waiting for decision-svc to become healthy ==="
for i in $(seq 1 30); do
  status="$(docker compose ps decision-svc --format '{{.Health}}' 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    echo "decision-svc is healthy (after ${i}x2s)"
    break
  fi
  sleep 2
done

echo "--- decision E2E (auto-approve through two services) ---"
for i in $(seq 1 30); do
  STATUS=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "approved" ] && break
  sleep 1
done
[ "$STATUS" = "approved" ] || { echo "FAIL: expected approved, got $STATUS"; exit 1; }
ROUTE=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['route'])")
[ "$ROUTE" = "auto_approve" ] || { echo "FAIL: route=$ROUTE"; exit 1; }
curl -sf http://localhost:8001/api/dashboard | grep -q '"decided_auto_approve"' \
  || { echo "FAIL: dashboard missing decided_auto_approve"; exit 1; }
# duplicate short-circuit across services: resubmit the same payload, expect duplicate
TRACK2=$(curl -sf -X POST http://localhost:8001/api/invoices -H 'Content-Type: application/json' \
  -d "$PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")
for i in $(seq 1 30); do
  S2=$(curl -sf "http://localhost:8001/api/invoices/$TRACK2" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$S2" = "duplicate" ] && break
  sleep 1
done
[ "$S2" = "duplicate" ] || { echo "FAIL: duplicate expected, got $S2"; exit 1; }
echo "DECISION E2E: OK"

echo "=== curl http://localhost:8002/api/config/thresholds ==="
curl -sf http://localhost:8002/api/config/thresholds | grep -q 25000 \
  || { echo "FAIL: thresholds endpoint missing expected ceiling_cents"; exit 1; }

echo "=== docker compose down ==="
docker compose down
