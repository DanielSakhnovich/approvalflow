#!/usr/bin/env bash
# D5 — one-command verification. Brings the whole stack up from cold, runs the
# four acceptance journeys and the two anti-cheese guards THROUGH THE GATEWAY
# (:8080, the single entry point), records every result, and prints a single
# PASS/FAIL. Exits 0 iff every check passed.
#
#   make verify        (or: bash scripts/verify.sh)
#
# Distinct from scripts/smoke-compose.sh (the developer run that also does live
# Dapr component characterization); this is the focused graded artifact.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

GW="http://localhost:8080"
PASS=(); FAIL=()

# Always tear the stack down on exit — including Ctrl-C mid-run, not only on
# normal completion.
trap 'echo "=== tearing down ==="; docker compose down -v >/dev/null 2>&1 || true' EXIT

pass() { PASS+=("$1"); printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { FAIL+=("$1"); printf '  \033[31m✗\033[0m %s\n' "$1"; }
check() { # check "<name>" <0-or-1 already-evaluated>; use with: check "x" "$cond"
  if [ "$2" = "1" ]; then pass "$1"; else fail "$1"; fi
}

# --- fixture payloads derived at runtime (no drift) -------------------------
payload() { # payload <fixture-id> [keep-scenario]
  python3 - "$1" "${2:-}" <<'PY'
import json, sys
fid, keep = sys.argv[1], sys.argv[2]
data = json.load(open("sample-invoices.json"))
inv = next(f for f in data["fixtures"] if f["id"] == fid)
drop = {"expected"} if keep == "scenario" else {"expected", "scenario"}
print(json.dumps({k: v for k, v in inv.items() if k not in drop}))
PY
}
submit() { curl -sf -X POST "$GW/api/invoices" -H 'Content-Type: application/json' \
  -d "$1" | python3 -c "import sys,json; print(json.load(sys.stdin)['trackingId'])"; }
field() { curl -sf "$GW/api/invoices/$1" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('$2',''))"; }
poll_status() { # poll_status <id> <target> [tries]
  local id="$1" target="$2" tries="${3:-40}" s=""
  for _ in $(seq 1 "$tries"); do
    s="$(field "$id" status)"; [ "$s" = "$target" ] && { echo "$s"; return 0; }
    sleep 2
  done
  echo "$s"; return 1
}
in_queue() { curl -sf "$GW/api/approvals/queue" | grep -q "$1"; }
verdict() { curl -sf -X POST "$GW/api/approvals/$1/verdict" -H 'Content-Type: application/json' \
  -d "{\"verdict\":\"$2\",\"approver_id\":\"lena.schmidt@northwind.example\",\"comment\":\"verify\"}" \
  >/dev/null; }

# --- bring the stack up cold ------------------------------------------------
echo "=== D5 verification: bringing the stack up (cold) ==="
[ -f dapr/secrets.json ] || cp dapr/secrets.example.json dapr/secrets.json
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
docker compose up --build -d >/dev/null 2>&1
for svc in intake-api decision-svc approval-svc payment-svc audit-svc notification-svc gateway; do
  for _ in $(seq 1 45); do
    [ "$(docker compose ps "$svc" --format '{{.Health}}' 2>/dev/null || true)" = "healthy" ] && break
    # gateway has no healthcheck; treat a served root as ready
    [ "$svc" = "gateway" ] && curl -sf "$GW/" >/dev/null 2>&1 && break
    sleep 2
  done
done
echo

echo "=== Journeys ==="

# Journey A — auto-approve -> paid, no human (INV-1001).
A_PAY="$(payload INV-1001)"
A_ID="$(submit "$A_PAY")"
A_STATUS="$(poll_status "$A_ID" paid)"
A_ROUTE="$(field "$A_ID" route)"
check "Journey A: INV-1001 auto-approves and is paid (no human)" \
  "$([ "$A_STATUS" = paid ] && [ "$A_ROUTE" = auto_approve ] && echo 1 || echo 0)"

# Journey B — escalate -> RESTART approval-svc -> resume (INV-1003, M11).
B_ID="$(submit "$(payload INV-1003)")"
poll_status "$B_ID" pending_approval >/dev/null || true
for _ in $(seq 1 45); do in_queue "$B_ID" && break; sleep 2; done
docker compose restart approval-svc >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(docker compose ps approval-svc --format '{{.Health}}')" != healthy ] && break; sleep 0.5; done
for _ in $(seq 1 30); do [ "$(docker compose ps approval-svc --format '{{.Health}}')" = healthy ] && break; sleep 1; done
docker compose restart approval-svc-dapr >/dev/null 2>&1
for _ in $(seq 1 30); do curl -sf http://localhost:8003/healthz >/dev/null 2>&1 && break; sleep 1; done
QUEUE_SURVIVED=0
for _ in $(seq 1 45); do in_queue "$B_ID" && { QUEUE_SURVIVED=1; break; }; sleep 2; done
verdict "$B_ID" approved || true
# INV-1003 carries no failure scenario and sales-2026Q2 has ample budget, so on
# approval it flows past the transient `approved` hop through to `paid`. Poll for
# `paid` (the true terminal state) rather than the racy intermediate `approved`:
# it deterministically proves BOTH the M11 resume AND that payment completed.
B_STATUS="$(poll_status "$B_ID" paid)"
check "Journey B: INV-1003 escalates, survives an approval-svc RESTART, resumes to paid on approve (M11)" \
  "$([ "$QUEUE_SURVIVED" = 1 ] && [ "$B_STATUS" = paid ] && echo 1 || echo 0)"

# Journey C — duplicate paid once (INV-1007 = same vendor+number+total as INV-1001).
C_ID="$(submit "$(payload INV-1007)")"
C_STATUS="$(poll_status "$C_ID" duplicate)"
check "Journey C: INV-1007 (resubmission of INV-1001) short-circuits as duplicate — not paid twice" \
  "$([ "$C_STATUS" = duplicate ] && echo 1 || echo 0)"

# Journey D — payment failure -> compensation, no orphaned reservation (INV-1012).
ENG_BEFORE="$(curl -sf "$GW/api/budgets/engineering-2026Q2" | python3 -c "import sys,json;print(json.load(sys.stdin)['remaining_cents'])")"
D_ID="$(submit "$(payload INV-1012 scenario)")"
poll_status "$D_ID" pending_approval >/dev/null || true
for _ in $(seq 1 45); do in_queue "$D_ID" && break; sleep 2; done
verdict "$D_ID" approved || true
D_STATUS="$(poll_status "$D_ID" payment_failed)"
ENG_AFTER="$(curl -sf "$GW/api/budgets/engineering-2026Q2" | python3 -c "import sys,json;print(json.load(sys.stdin)['remaining_cents'])")"
# Guard against a vacuous pass: if the budgets endpoint were unreachable at BOTH
# samples, curl -sf yields nothing and `[ "" = "" ]` would be true — "restored"
# on no evidence. Require ENG_BEFORE to be a real integer before trusting equality.
D_BUDGET_OK=0
case "$ENG_BEFORE" in ''|*[!0-9]*) ;; *) [ "$ENG_BEFORE" = "$ENG_AFTER" ] && D_BUDGET_OK=1;; esac
check "Journey D: INV-1012 payment fails, saga compensates, budget restored (no orphaned reservation)" \
  "$([ "$D_STATUS" = payment_failed ] && [ "$D_BUDGET_OK" = 1 ] && echo 1 || echo 0)"

echo
echo "=== Anti-cheese guards ==="

# (a) at least 2 items auto-approve with NO human.
G1_ID="$(submit "$(payload INV-1016)")"
G2_ID="$(submit "$(payload INV-1017)")"
G1="$(poll_status "$G1_ID" paid)"; G2="$(poll_status "$G2_ID" paid)"
G1R="$(field "$G1_ID" route)"; G2R="$(field "$G2_ID" route)"
AUTO_NO_HUMAN=0
[ "$G1" = paid ] && [ "$G1R" = auto_approve ] && AUTO_NO_HUMAN=$((AUTO_NO_HUMAN+1))
[ "$G2" = paid ] && [ "$G2R" = auto_approve ] && AUTO_NO_HUMAN=$((AUTO_NO_HUMAN+1))
[ "$A_STATUS" = paid ] && [ "$A_ROUTE" = auto_approve ] && AUTO_NO_HUMAN=$((AUTO_NO_HUMAN+1))  # INV-1001 too
check "Anti-cheese (a): >= 2 items auto-approve with NO human ($AUTO_NO_HUMAN auto-approvals)" \
  "$([ "$AUTO_NO_HUMAN" -ge 2 ] && echo 1 || echo 0)"

# (b) an "approve me" note does NOT flip the decision (INV-1013, $300, over ceiling).
S_ID="$(submit "$(payload INV-1013)")"
S_STATUS="$(poll_status "$S_ID" pending_approval)"
S_ROUTE="$(field "$S_ID" route)"
check "Anti-cheese (b): INV-1013 \"Approve me\" note does NOT flip — routes human_review, not auto_approve" \
  "$([ "$S_ROUTE" = human_review ] && [ "$S_STATUS" != paid ] && echo 1 || echo 0)"

# (c) F10: no auto-approval ever exceeded its ceiling, even after all of the above.
# One fetch, read twice — avoids a needless second call and any race between them.
COMPLIANCE="$(curl -sf "$GW/api/audit/ceiling-compliance")"
VIOL="$(printf '%s' "$COMPLIANCE" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['violations']))")"
CHECKED="$(printf '%s' "$COMPLIANCE" | python3 -c "import sys,json;print(json.load(sys.stdin)['autoApprovalsChecked'])")"
check "Anti-cheese (c): F10 — $CHECKED auto-approvals checked, $VIOL ceiling violations (must be 0)" \
  "$([ "$VIOL" = 0 ] && [ "$CHECKED" -ge 1 ] && echo 1 || echo 0)"

# --- summary ----------------------------------------------------------------
echo
TOTAL=$(( ${#PASS[@]} + ${#FAIL[@]} ))
if [ "${#FAIL[@]}" -eq 0 ]; then
  printf '\033[32m=== VERIFICATION: PASS (%d/%d) ===\033[0m\n' "${#PASS[@]}" "$TOTAL"
  RC=0
else
  printf '\033[31m=== VERIFICATION: FAIL (%d/%d passed) ===\033[0m\n' "${#PASS[@]}" "$TOTAL"
  printf 'failed:\n'; for f in "${FAIL[@]}"; do printf '  - %s\n' "$f"; done
  RC=1
fi

# Teardown runs from the EXIT trap (covers Ctrl-C too).
exit "$RC"
