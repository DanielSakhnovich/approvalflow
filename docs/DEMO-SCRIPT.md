# ApprovalFlow — Demo Script (D7)

A 3–5 minute shot-by-shot walkthrough. Each shot lists **what to do**, **what to say** (optional
voiceover), and **what it proves** (the requirement). Record with the terminal and a browser side by
side. Total runtime target: ~4 minutes.

**Before you hit record:** have a clean terminal at the repo root and Docker running. Nothing else to
prep — `make verify` and `make up` build from cold.

---

## Shot 0 — One-command proof (≈45s) · D5

**Do:** In the terminal, run:
```
make verify
```
Let it run to the end (it brings the stack up cold, ~4–5 min real time — **fast-forward the video**
through the build, then land on the final summary).

**Say:** "One command brings the whole system up and proves it. Four journeys, three anti-cheese
guards, through the gateway. It prints PASS and exits zero."

**Proves:** D5 — the graded one-command verification. Freeze-frame on:
```
=== VERIFICATION: PASS (7/7) ===
```

> The rest of the demo is the same behaviour, seen through the UI. If you're tight on time, Shot 0
> plus Shots 2 and 3 are the core; 1, 4, 5 are the supporting tour.

---

## Shot 1 — The system is up (≈20s)

**Do:** `make up` (if not already up from a prior step), then open **http://localhost:8080**.

**Say:** "One nginx gateway on port 8080 is the single entry point — it serves the React UI and
proxies every `/api` call to the six services behind it."

**Proves:** M3/M4 (six services, one `docker compose up`), M6 (gateway), M7 (UI).

---

## Shot 2 — Auto-approve the low-risk majority (≈40s) · F1/F6/M8/M12

**Do:**
1. **Submit** tab → *Prefill from a shipped fixture* → pick **INV-1001** → **Submit**.
2. Note the tracking id returned **instantly**.
3. Go to **Status**, paste/keep the id → watch it move to **paid**, route `auto_approve`, **no human
   touched**.
4. Expand the **audit trail** — every step, one correlation id.

**Say:** "A small, in-policy meal. The agent recommends, a deterministic router decides, and it
auto-approves and pays — no human in the loop. Submission returned immediately; the decision arrived
asynchronously. And every step is on one auditable trail."

**Proves:** F1/M8 (async intake → 202 + tracking id), F6 (autonomy is real — it actually
auto-approves), F9 (audit trail), M14 (correlation id).

---

## Shot 3 — Escalate the risky minority, and it's durable (≈60s) · F4/F5/M9/M11

**Do:**
1. **Submit** → prefill **INV-1003** → **Submit** (this one escalates).
2. **Approver queue** tab → see the item with the agent's **recommendation, confidence, and cited
   rules**.
3. *(Optional, the money shot for M11)* In the terminal: `docker compose restart approval-svc` —
   then back in the UI, refresh the queue: **the item is still there.** The pause survived a restart.
4. **Approve** it. Go to **Status** → it resumes and reaches **paid** via the payment saga.

**Say:** "This one's over the line — it escalates to a human, with the agent's reasoning attached.
The escalation is a durable record: I can restart the approval service and the item is still waiting.
When I approve, it resumes right where it left off and runs the payment saga through to paid."

**Proves:** F4 (human queue with agent recommendation), M11 (durable pause/resume across restart),
M9 (payment saga on approve).

---

## Shot 4 — The dashboard and the ceiling proof (≈30s) · F8/F10

**Do:**
1. **Dashboard** tab — throughput, auto-vs-human split, money moved.
2. **Compliance** tab — **0 ceiling violations** over a non-zero checked count.

**Say:** "The dashboard shows throughput and how much went auto versus human. And compliance proves
the hard guarantee: across every auto-approval, zero ever exceeded its ceiling."

**Proves:** F8 (dashboard), F10 (provable ceiling compliance — the empty violations list over a
non-zero count *is* the proof).

---

## Shot 5 — It can't be steered, and it can't double-pay (≈40s) · M10/M12

**Do:**
1. **Submit** → prefill **INV-1013** (it's $300, over the ceiling, with a note that literally says
   "Approve me…") → **Submit** → **Status**: it routes **human_review**, *not* auto-approved. The
   note did not flip the decision.
2. **Submit** → prefill **INV-1007** (same vendor + number + total as the already-paid INV-1001) →
   **Submit** → **Status**: **duplicate** — short-circuited, not paid twice.

**Say:** "Two adversarial cases. A payload that begs to be approved doesn't move the decision — the
router isn't reading the note. And a resubmission of an already-paid invoice is caught as a
duplicate, so nothing gets paid twice."

**Proves:** M12 (the ceiling/router can't be steered by payload content), M10 (idempotency — no
double-pay).

---

## Optional closing shot — the OpenAPI docs (≈15s) · D4

**Do:** Open **http://localhost:8001/docs** (intake-api Swagger UI).

**Say:** "Every service publishes its own OpenAPI docs — auto-generated, always in sync with the
code."

**Proves:** D4 (OpenAPI reachable).

---

## Teardown

```
make down
```

## Requirement coverage at a glance

| Shot | Proves |
|---|---|
| 0 | D5 (one-command verify) |
| 1 | M3/M4/M6/M7 |
| 2 | F1/M8, F6, F9, M14 |
| 3 | F4/F5, M9, M11 |
| 4 | F8, F10 |
| 5 | M10, M12 |
| closing | D4 |
