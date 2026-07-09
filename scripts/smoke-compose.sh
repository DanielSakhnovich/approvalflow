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

# Wait for payment-svc (and give its daprd sidecar a chance to register its
# /dapr/subscribe topics) BEFORE any invoice is submitted below. Dapr's redis
# pubsub component creates each app's consumer group at subscribe time; a
# consumer group created AFTER a message was published does not see that
# message (this is a cold run -- `docker compose down -v` wiped any prior
# groups). Blocking here, before INV-1001 is ever submitted, guarantees
# payment-svc is already subscribed by the time decision-svc later publishes
# decision-made for it -- the same ordering hazard that would otherwise
# silently strand journey A short of `paid`.
# All event consumers must be subscribed BEFORE the first invoice flows, or a
# redis-pubsub consumer group created after publish misses the message (same
# cold-run ordering hazard as payment-svc). audit-svc + notification-svc are
# leaf consumers, so wait for every consumer here up front.
for svc in payment-svc audit-svc notification-svc; do
  echo "=== waiting for $svc to become healthy ==="
  for i in $(seq 1 30); do
    status="$(docker compose ps "$svc" --format '{{.Health}}' 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
      echo "$svc is healthy (after ${i}x2s)"
      break
    fi
    sleep 2
  done
done

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

# D-017 GATE: characterize the Postgres audit state component's CAS/ETag
# behavior against afcommon's DaprStateStore -- exactly the discipline that
# de-risked Redis above. The audit append path is a cas_update loop, so it
# only trusts this component once first-write + stale-etag semantics are
# confirmed live. Exec'd inside audit-svc (its sidecar mounts statestore-audit).
echo "=== Postgres audit-store CAS characterization (exec'd inside audit-svc) ==="
docker compose exec -T audit-svc python -c "
import asyncio, sys
sys.path.insert(0, '/opt/afcommon')
from afcommon.state import DaprStateStore

async def main():
    s = DaprStateStore(store_name='statestore-audit')
    # first-write on a fresh key
    assert await s.try_save('pg-char', {'n': 1}, None), 'first write should succeed'
    v, etag = await s.get('pg-char')
    assert v == {'n': 1} and etag, ('unexpected read', v, etag)
    print(f'PG-CHAR first-write ok, etag={etag!r}')
    # fresh etag save succeeds
    assert await s.try_save('pg-char', {'n': 2}, etag), 'fresh-etag save should succeed'
    # stale etag (the original) must now be rejected
    stale = await s.try_save('pg-char', {'n': 3}, etag)
    assert stale is False, 'stale-etag save must be rejected (got True)'
    print('PG-CHAR stale-etag correctly rejected -> try_save False')
    # first-write-only against an existing key must be rejected
    fw = await s.try_save('pg-char', {'n': 4}, None)
    assert fw is False, 'first-write-only on existing key must be rejected (got True)'
    print('PG-CHAR first-write-only on existing key correctly rejected')
    # append via cas_update (the real audit path) works end-to-end
    from afcommon.state import cas_update
    await cas_update(s, 'pg-list', lambda cur: (cur or []) + ['a'])
    await cas_update(s, 'pg-list', lambda cur: (cur or []) + ['b'])
    lst, _ = await s.get('pg-list')
    assert lst == ['a', 'b'], ('cas append lost data', lst)
    print('PG-CHAR cas_update append ok ->', lst)
    print('POSTGRES CAS CHARACTERIZATION: OK')

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
# With payment-svc running, an auto_approve invoice does not rest at `approved`:
# payment carries it on to `paid` within a second or two. Accept either state
# here (the route assertion below is the real decision check); the dedicated
# journey-A-completion section then confirms it reaches `paid`.
for i in $(seq 1 30); do
  STATUS=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  { [ "$STATUS" = "approved" ] || [ "$STATUS" = "paid" ]; } && break
  sleep 1
done
{ [ "$STATUS" = "approved" ] || [ "$STATUS" = "paid" ]; } \
  || { echo "FAIL: expected approved or paid, got $STATUS"; exit 1; }
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

echo "--- journey A completion: INV-1001 auto-approve -> paid (money moves) ---"
for i in $(seq 1 30); do
  STATUS=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "paid" ] && break
  sleep 1
done
[ "$STATUS" = "paid" ] || { echo "FAIL: expected paid, got $STATUS"; exit 1; }
curl -sf http://localhost:8001/api/dashboard | python3 -c "
import sys, json
d = json.load(sys.stdin)
paid_auto = d.get('paid_auto_cents')
assert paid_auto == 4200, f'expected paid_auto_cents=4200, got {paid_auto!r}: {d}'
print('dashboard paid_auto_cents == 4200: OK')
"
echo "JOURNEY A (paid): OK"

echo "=== curl http://localhost:8002/api/config/thresholds ==="
curl -sf http://localhost:8002/api/config/thresholds | grep -q 25000 \
  || { echo "FAIL: thresholds endpoint missing expected ceiling_cents"; exit 1; }

echo "=== waiting for approval-svc to become healthy ==="
for i in $(seq 1 30); do
  status="$(docker compose ps approval-svc --format '{{.Health}}' 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    echo "approval-svc is healthy (after ${i}x2s)"
    break
  fi
  sleep 2
done

echo "--- approval E2E: escalate -> RESTART -> resume (M11 / journey B) ---"
ESC_PAYLOAD=$(python3 - <<'EOF'
import json
data = json.load(open("sample-invoices.json"))
inv = next(f for f in data["fixtures"] if f["id"] == "INV-1003")
inv = {k: v for k, v in inv.items() if k not in ("expected", "scenario")}
print(json.dumps(inv))
EOF
)
TRACK3=$(curl -sf -X POST http://localhost:8001/api/invoices -H 'Content-Type: application/json' \
  -d "$ESC_PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")
for i in $(seq 1 30); do
  S3=$(curl -sf "http://localhost:8001/api/invoices/$TRACK3" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$S3" = "pending_approval" ] && break; sleep 1
done
[ "$S3" = "pending_approval" ] || { echo "FAIL: expected pending_approval, got $S3"; exit 1; }
for i in $(seq 1 45); do
  curl -sf http://localhost:8003/api/approvals/queue | grep -q "$TRACK3" && break; sleep 2
done
curl -sf http://localhost:8003/api/approvals/queue | grep -q "$TRACK3" || { echo "FAIL: not in queue"; exit 1; }

echo "--- restarting approval-svc (the M11 moment) ---"
# `docker compose restart approval-svc approval-svc-dapr` as a single command
# fires kill/stop/start for both containers concurrently (confirmed via
# `docker events`: both timestamps identical to the second). The sidecar
# (network_mode: "service:approval-svc") re-attaches to whatever network
# namespace approval-svc has AT THE MOMENT the sidecar container itself
# restarts -- and since daprd's own restart is much faster than the
# Python/uvicorn app's, the sidecar reliably finishes (and re-joins the
# namespace) *before* approval-svc has torn down and recreated its own
# namespace, permanently orphaning the sidecar (httpx.ConnectError inside
# the app forever after -- not a transient race that more waiting fixes).
# Independently reproduced during review (2026-07-07): after the joint
# restart, daprd logged a clean startup (placement connected) yet
# localhost:3500 stayed connection-refused from inside the app container
# 45+ seconds later -- daprd's sockets live in the app's dead pre-restart
# namespace. Restarting approval-svc, waiting for it to
# be healthy again, and only then restarting its sidecar is the same real
# restart (both containers really do restart) made deterministic instead of
# racy; this is not a sleep-based workaround for a flaky assertion, it's
# sequencing around a genuine docker network_mode:"service:X" hazard shared
# by every service in this compose file.
#
# `docker compose ps --format Health` can still read the PRE-restart
# "healthy" status for an instant right after `restart` is issued (the
# container's health state hasn't flipped away from healthy yet), so a
# naive "wait until healthy" loop can false-positive on stale state and
# return immediately -- observed directly: t=1 poll after issuing restart
# still reported "healthy" before dropping to "starting". Waiting for the
# status to first LEAVE healthy, then waiting for it to become healthy
# again, closes that window.
docker compose restart approval-svc
for i in $(seq 1 30); do
  status="$(docker compose ps approval-svc --format '{{.Health}}' 2>/dev/null || true)"
  [ "$status" != "healthy" ] && break
  sleep 0.5
done
for i in $(seq 1 30); do
  status="$(docker compose ps approval-svc --format '{{.Health}}' 2>/dev/null || true)"
  [ "$status" = "healthy" ] && break
  sleep 1
done
docker compose restart approval-svc-dapr
for i in $(seq 1 30); do
  curl -sf http://localhost:8003/healthz >/dev/null 2>&1 && break; sleep 1
done
# /healthz is a static handler — it passing does NOT mean the restarted sidecar
# has reconnected to Redis yet, but the queue endpoint (which reads Dapr state)
# needs that. Poll the queue itself: the escalation was durably written to Redis
# BEFORE the restart, so if it were truly lost no amount of polling recovers it;
# this only waits out the sidecar's reconnect, then still fails hard if absent.
for i in $(seq 1 30); do
  curl -sf http://localhost:8003/api/approvals/queue 2>/dev/null | grep -q "$TRACK3" && break
  sleep 1
done
curl -sf http://localhost:8003/api/approvals/queue | grep -q "$TRACK3" \
  || { echo "FAIL: queue lost across restart — M11 broken"; exit 1; }

curl -sf -X POST "http://localhost:8003/api/approvals/$TRACK3/verdict" \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"approved","approver_id":"lena.schmidt@northwind.example","comment":"client name confirmed offline"}' >/dev/null \
  || { echo "FAIL: verdict rejected"; exit 1; }
for i in $(seq 1 30); do
  S3=$(curl -sf "http://localhost:8001/api/invoices/$TRACK3" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$S3" = "approved" ] && break; sleep 1
done
[ "$S3" = "approved" ] || { echo "FAIL: resume did not reach intake, got $S3"; exit 1; }
echo "APPROVAL E2E (M11 restart survived): OK"

echo "--- journey D (INV-1012): human-approved -> injected payment failure -> compensation ---"
D_PAYLOAD=$(python3 - <<'EOF'
import json
data = json.load(open("sample-invoices.json"))
inv = next(f for f in data["fixtures"] if f["id"] == "INV-1012")
# KEEP scenario: payment-svc's failure injection is double-gated on
# FAILURE_INJECTION_ENABLED=true (compose env) AND this fixture's
# scenario startswith "payment-failure" -- journey D needs it to ride the
# invoice dict all the way through decision-made/approval-resolved.
inv = {k: v for k, v in inv.items() if k != "expected"}
print(json.dumps(inv))
EOF
)

ENG_BEFORE=$(curl -sf http://localhost:8004/api/budgets/engineering-2026Q2 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['remaining_cents'])")
echo "engineering-2026Q2 remaining before journey D: $ENG_BEFORE cents"

TRACK_D=$(curl -sf -X POST http://localhost:8001/api/invoices -H 'Content-Type: application/json' \
  -d "$D_PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")
for i in $(seq 1 30); do
  SD=$(curl -sf "http://localhost:8001/api/invoices/$TRACK_D" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$SD" = "pending_approval" ] && break
  sleep 1
done
[ "$SD" = "pending_approval" ] || { echo "FAIL: journey D expected pending_approval, got $SD"; exit 1; }
# intake and approval both consume decision-made independently; intake can flip
# to pending_approval before approval finishes writing the escalation + queue
# index, so poll the queue rather than single-shot grep.
for i in $(seq 1 45); do
  curl -sf http://localhost:8003/api/approvals/queue | grep -q "$TRACK_D" && break
  sleep 2  # space out: the queue GET self-heals every id, and hammering it
           # starves approval-svc's event loop from creating this escalation
done
curl -sf http://localhost:8003/api/approvals/queue | grep -q "$TRACK_D" \
  || { echo "FAIL: journey D invoice not in approval queue"; exit 1; }

curl -sf -X POST "http://localhost:8003/api/approvals/$TRACK_D/verdict" \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"approved","approver_id":"lena.schmidt@northwind.example","comment":"capital hardware approved"}' \
  >/dev/null || { echo "FAIL: journey D verdict rejected"; exit 1; }

for i in $(seq 1 30); do
  SD=$(curl -sf "http://localhost:8001/api/invoices/$TRACK_D" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$SD" = "payment_failed" ] && break
  sleep 1
done
[ "$SD" = "payment_failed" ] || { echo "FAIL: journey D expected payment_failed, got $SD"; exit 1; }

ENG_AFTER=$(curl -sf http://localhost:8004/api/budgets/engineering-2026Q2 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['remaining_cents'])")
# Reservation was released on compensation -- assert unchanged relative to
# the pre-journey-D snapshot, NOT the raw seeded total: engineering-2026Q2
# already absorbed INV-1001's $42.00 payment (journey A, above), so the
# live remaining is seeded-minus-4200, not the fixture's seeded value.
[ "$ENG_AFTER" = "$ENG_BEFORE" ] \
  || { echo "FAIL: engineering budget changed by journey D: before=$ENG_BEFORE after=$ENG_AFTER"; exit 1; }
echo "JOURNEY D (compensated, engineering remaining unchanged at $ENG_AFTER cents): OK"

echo "--- INV-1014A/B concurrency: no-overspend on marketing-2026Q2 (\$1,000 budget, two \$600 claims) ---"
A_PAYLOAD=$(python3 - <<'EOF'
import json
data = json.load(open("sample-invoices.json"))
inv = next(f for f in data["fixtures"] if f["id"] == "INV-1014A")
inv = {k: v for k, v in inv.items() if k not in ("expected", "scenario")}
print(json.dumps(inv))
EOF
)
B_PAYLOAD=$(python3 - <<'EOF'
import json
data = json.load(open("sample-invoices.json"))
inv = next(f for f in data["fixtures"] if f["id"] == "INV-1014B")
inv = {k: v for k, v in inv.items() if k not in ("expected", "scenario")}
print(json.dumps(inv))
EOF
)

TRACK_A=$(curl -sf -X POST http://localhost:8001/api/invoices -H 'Content-Type: application/json' \
  -d "$A_PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")
TRACK_B=$(curl -sf -X POST http://localhost:8001/api/invoices -H 'Content-Type: application/json' \
  -d "$B_PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])")

for TRK in "$TRACK_A" "$TRACK_B"; do
  S14=""
  for i in $(seq 1 30); do
    S14=$(curl -sf "http://localhost:8001/api/invoices/$TRK" \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    [ "$S14" = "pending_approval" ] && break
    sleep 1
  done
  [ "$S14" = "pending_approval" ] || { echo "FAIL: $TRK expected pending_approval, got $S14"; exit 1; }
done
# Poll until BOTH are in the queue (approval writes the escalation slightly
# after intake flips status; see journey D note).
for i in $(seq 1 45); do
  Q=$(curl -sf http://localhost:8003/api/approvals/queue)
  echo "$Q" | grep -q "$TRACK_A" && echo "$Q" | grep -q "$TRACK_B" && break
  sleep 2
done
Q=$(curl -sf http://localhost:8003/api/approvals/queue)
echo "$Q" | grep -q "$TRACK_A" || { echo "FAIL: $TRACK_A not in approval queue"; exit 1; }
echo "$Q" | grep -q "$TRACK_B" || { echo "FAIL: $TRACK_B not in approval queue"; exit 1; }

# Approve BOTH in parallel (background curl): the race we care about is
# INSIDE payment-svc's budget CAS loop, not in how fast this shell issues
# two sequential requests -- firing them concurrently gives the saga its
# best chance to actually contend.
curl -sf -X POST "http://localhost:8003/api/approvals/$TRACK_A/verdict" \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"approved","approver_id":"lena.schmidt@northwind.example","comment":"booth deposit"}' \
  >/dev/null &
PID_A=$!
curl -sf -X POST "http://localhost:8003/api/approvals/$TRACK_B/verdict" \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"approved","approver_id":"lena.schmidt@northwind.example","comment":"booth balance"}' \
  >/dev/null &
PID_B=$!
wait "$PID_A" || { echo "FAIL: verdict A rejected"; exit 1; }
wait "$PID_B" || { echo "FAIL: verdict B rejected"; exit 1; }

SA="" SB=""
for i in $(seq 1 30); do
  SA=$(curl -sf "http://localhost:8001/api/invoices/$TRACK_A" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  SB=$(curl -sf "http://localhost:8001/api/invoices/$TRACK_B" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  case "$SA,$SB" in
    paid,payment_failed|payment_failed,paid) break ;;
  esac
  sleep 1
done
if { [ "$SA" = "paid" ] && [ "$SB" = "payment_failed" ]; } \
  || { [ "$SA" = "payment_failed" ] && [ "$SB" = "paid" ]; }; then
  echo "exactly one paid, one payment_failed: A=$SA B=$SB"
else
  echo "FAIL: expected exactly one paid + one payment_failed, got A=$SA B=$SB"; exit 1
fi

MKT_REMAINING=$(curl -sf http://localhost:8004/api/budgets/marketing-2026Q2 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['remaining_cents'])")
[ "$MKT_REMAINING" = "40000" ] \
  || { echo "FAIL: marketing-2026Q2 remaining expected 40000, got $MKT_REMAINING"; exit 1; }
echo "INV-1014A/B (no-overspend, marketing-2026Q2 remaining=$MKT_REMAINING cents): OK"

echo "--- audit trail (F9): full chain for journey A's INV-1001 ---"
# Fetch the trail through intake's ?trail=true (which itself invokes audit via
# Dapr, exercising the M5 leg too). Poll until the terminal event is recorded.
for i in $(seq 1 30); do
  TRAIL=$(curl -sf "http://localhost:8001/api/invoices/$TRACKING?trail=true")
  echo "$TRAIL" | grep -q "payment-completed" && break
  sleep 2
done
echo "$TRAIL" | grep -q "invoice-submitted" \
  || { echo "FAIL: trail missing invoice-submitted"; exit 1; }
echo "$TRAIL" | grep -q "decision-made" \
  || { echo "FAIL: trail missing decision-made"; exit 1; }
echo "$TRAIL" | grep -q "payment-completed" \
  || { echo "FAIL: trail missing payment-completed"; exit 1; }
echo "AUDIT TRAIL F9 (full chain: submitted -> decision -> paid): OK"

echo "--- ?trail=true (M5 sync leg: intake -> audit via Dapr invoke) ---"
TRAIL_LEN=$(echo "$TRAIL" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('trail',[])))")
[ "$TRAIL_LEN" -ge 3 ] \
  || { echo "FAIL: ?trail=true returned $TRAIL_LEN entries, expected >= 3"; exit 1; }
echo "M5 SYNC LEG (?trail=true returned $TRAIL_LEN entries via Dapr invoke): OK"

echo "--- ceiling compliance (F10): no auto-approval ever exceeded its ceiling ---"
COMPLIANCE=$(curl -sf http://localhost:8005/audit/ceiling-compliance)
CHECKED=$(echo "$COMPLIANCE" | python3 -c "import sys,json; print(json.load(sys.stdin)['autoApprovalsChecked'])")
VIOLATIONS=$(echo "$COMPLIANCE" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['violations']))")
[ "$CHECKED" -ge 1 ] \
  || { echo "FAIL: F10 checked $CHECKED auto-approvals, expected >= 1"; exit 1; }
[ "$VIOLATIONS" = "0" ] \
  || { echo "FAIL: F10 found $VIOLATIONS ceiling violations, expected 0"; exit 1; }
echo "F10 (checked $CHECKED auto-approvals, 0 violations -- the empty list IS the proof): OK"

echo "--- notification: submitters were told their outcomes ---"
for i in $(seq 1 30); do
  N=$(curl -sf http://localhost:8006/notifications \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for n in d if n.get('status')=='sent'))")
  [ "${N:-0}" -ge 1 ] && break
  sleep 2
done
[ "${N:-0}" -ge 1 ] \
  || { echo "FAIL: no 'sent' notifications, expected >= 1"; exit 1; }
echo "NOTIFICATION (>=1 outcome delivered, $N sent): OK"

echo "--- AOF sanity ---"
docker compose exec -T redis redis-cli CONFIG GET appendonly | grep -q yes \
  || { echo "FAIL: redis appendonly not enabled"; exit 1; }
echo "AOF SANITY: OK"

echo "=== docker compose down ==="
docker compose down
