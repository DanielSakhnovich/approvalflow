# Phase 06 — Audit + Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two leaf consumers close the system. **audit-svc** records an immutable, append-only decision trail per correlation id on a **Postgres**-backed Dapr state store (D-017), answering F9 (full trail) and F10 (prove no auto-approval ever exceeded its ceiling), and serves intake's `?trail=true` view via **Dapr service invocation** — the system's one synchronous service-to-service call (M5). **notification-svc** vendors Daniel's HW3 notification domain code (D-006) behind a thin FastAPI + afcommon adapter, subscribing to terminal events and "delivering" outcomes via mock providers.

**Architecture:** Spec §7, decisions D-005 (dedicated immutable audit svc), D-006 (reuse notification), D-015 (own keys), D-016 (afcommon dedupe), D-017 (audit→Postgres, its component CAS characterized in the smoke like Redis was in Phase 01). **Carry from Phase 05:** payment publishes terminal events AFTER its saga marker, so a crash between marker and publish can drop a payment-completed/failed — audit MUST NOT assume exactly-once receipt; an append-only trail with per-event dedupe tolerates this (a missing event is a gap, never a corruption).

**Tech Stack:** Python 3.12 · FastAPI · pydantic v2 · afcommon · Dapr pub/sub + state (Redis for ops, **Postgres for audit**) + service invocation · vendored Flask-era notification domain modules.

## Global Constraints

- Audit trail is APPEND-ONLY: events are added to `trail:{correlation_id}` via `cas_update`; no event is ever overwritten or removed. Ordering by the event's `occurred_at`, tie-broken by arrival.
- Audit runs on the **audit** Dapr state component (`store_name="statestore-audit"`, type `state.postgresql`) — the SAME afcommon `DaprStateStore` class, different component name (proves D-017's "swap is config, not code"). Its CAS/ETag behavior is UNVERIFIED against this component until Task 5's smoke characterizes it (exactly like the Redis ETag characterization in Phase 01) — the append path must be validated live before trust.
- Consumer dedupe via afcommon `EventDedupe` (D-016); audit tolerates gaps (see carry above) but never double-appends a redelivered event.
- notification-svc storage stays in-memory (D-010's one documented exception — transient data); seeding disabled.
- Money/amounts are display-only in both services — never recomputed.
- Branch `feature/audit-notification` off `main`; conventional commits; controller pushes per task; PR at end. Baseline: **313 passed**. Relative imports in `src/`; no noqa; installed-package imports (CI guards).

---

### Task 1: Audit scaffold + Postgres component + append-only trail store (F9/F10)

**Files:**
- Create: `services/audit/` skeleton (init files, `src/main.py` healthz `audit-svc`, `requirements.txt`, `Dockerfile` — decision pattern, no policy/sample COPY), `src/trail.py`, `src/api.py`, `src/deps.py`
- Create: `dapr/components/statestore-audit.yaml` (type `state.postgresql`, connectionString to the `postgres` compose service, `actorStateStore: "false"`, table name `audit_state`)
- Modify: `docker-compose.yml` (add `postgres:16-alpine` container with healthcheck `pg_isready`, named volume `postgres-data`, POSTGRES_PASSWORD/DB env; NOT wired to app services yet — Task 5 adds audit-svc)
- Test: `services/audit/tests/test_healthz.py`, `services/audit/tests/test_trail.py`

**Interfaces:**
- `trail.py`: `TrailEntry` (pydantic): `event_type: str, event_id: str, occurred_at: str, payload: dict` · `AuditTrail(store: StateStore)` with:
  - `async append(correlation_id, entry: TrailEntry) -> None` — `cas_update("trail:{correlation_id}", lambda cur: (cur or []) + [entry.model_dump()])` (append-only; the CAS loop serializes concurrent appends for the same cid).
  - `async get_trail(correlation_id) -> list[TrailEntry]` — returns entries sorted by `occurred_at`; `[]` if none.
  - `async ceiling_violations() -> list[dict]` — F10: scan is not natural on a KV store, so audit maintains a SECONDARY index key `index:auto-approvals` (append `{correlation_id, invoice_id, usd_cents, ceiling_cents}` whenever a `decision-made` with `route=auto_approve` is appended); `ceiling_violations` reads that index and returns any entry where `usd_cents > ceiling_cents`. In a correct system this is ALWAYS `[]` — that empty result IS the F10 proof. (Document: the index is the queryable projection; the trail is the source of truth.)
- `api.py`: `GET /trail/{correlation_id}` → `{"correlationId", "entries": [...]}` (F9) · `GET /audit/ceiling-compliance` → `{"autoApprovalsChecked": N, "violations": [...]}` (F10; violations always empty). 404 semantics: an unknown correlation id returns `200` with `entries: []` (absence of trail is a valid answer, not an error).
- `deps.py`: `get_trail()` lazy singleton over `DaprStateStore(store_name="statestore-audit")`; DI-overridable.

- [ ] **Step 1: Failing tests** (InMemoryStateStore): append then get_trail roundtrip; multiple events ordered by occurred_at; append is additive (second append keeps the first); concurrent appends for one cid on YieldingStateStore → both retained (no lost append); ceiling_violations empty when all auto-approvals in-ceiling; ceiling_violations returns the bad one when an index entry has usd_cents > ceiling_cents (synthetic); unknown cid → empty trail. API tests via TestClient with DI override.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN + full suite + ruff.** (Postgres component itself is config-only here; the store is exercised with InMemory in unit tests, live in Task 5.)
- [ ] **Step 5: Commit** — `feat(audit): scaffold, append-only trail store, F9 trail + F10 ceiling-compliance (Postgres component)`

---

### Task 2: Audit subscriptions — every event lands in the trail

**Files:**
- Create: `services/audit/src/subscriptions.py`; Modify: `main.py` (mount)
- Test: `services/audit/tests/test_subscriptions.py`

**Interfaces:**
- `GET /dapr/subscribe` → all FIVE topics (`invoice-submitted`, `decision-made`, `approval-resolved`, `payment-completed`, `payment-failed`) → `/events/{topic}`.
- Each handler: `parse_cloudevent` → `bind_event_context` → `EventDedupe.first_time(event_id)` (skip+ack on duplicate) → build `TrailEntry(event_type=topic, event_id=meta.event_id, occurred_at=meta.occurred_at, payload=<the event payload dict>)` → `trail.append(meta.correlation_id, entry)` → for `decision-made` with `route=auto_approve` also append to the auto-approvals index → ack. Exception post-mark → `forget` → re-raise 500 (D-016 pattern). Audit NEVER filters by route/verdict — it records everything.
- One generic handler parametrized by topic (or five thin handlers delegating to a shared `_record(topic, event, repo, dedupe)`); keep it DRY.

- [ ] **Step 1: Failing tests:** subscribe lists all five; each event type appends a correctly-typed entry retrievable via get_trail; a decision-made auto_approve also lands in the ceiling index; redelivered event (same event_id) → single entry (dedupe); post-append failure → 500 + forget; a full simulated journey (invoice-submitted → decision-made → approval-resolved → payment-completed, shared correlation_id) → get_trail returns all four ordered.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN + gates.**
- [ ] **Step 5: Commit** — `feat(audit): dapr subscriptions record every event into the correlation trail (F9)`

---

### Task 3: M5 sync leg — intake `?trail=true` via Dapr service invocation

**Files:**
- Modify: `services/intake/src/api.py` (extend `GET /invoices/{invoice_id}` with optional `?trail=true`), `services/intake/src/deps.py` (add an audit-invoke client seam)
- Create: `services/intake/src/audit_client.py` (`async fetch_trail(correlation_id) -> list[dict]` calling `http://localhost:{dapr_port}/v1.0/invoke/audit-svc/method/trail/{correlation_id}` — Dapr SERVICE INVOCATION, the one sync service-to-service call; M5)
- Test: `services/intake/tests/test_trail_view.py`

**Interfaces:**
- `GET /invoices/{invoice_id}?trail=true` → the normal view PLUS `"trail": [...]` (the audit entries for the record's correlation_id, fetched via Dapr invoke). Without the flag, behavior is byte-identical to today (no audit call).
- `audit_client.fetch_trail`: on audit unreachable/5xx → return `[]` and log a warning (the status view must still render; the trail is best-effort enrichment, NOT fail-loud — a missing trail must not break status). Injected/overridable for tests (a fake returning canned entries; assert the real path builds the correct invoke URL).
- deps: `get_audit_client()` seam.

- [ ] **Step 1: Failing tests:** `?trail=true` with a fake audit client returning 2 entries → response has `trail` with those 2 (+ the normal fields intact); no flag → no `trail` key AND the audit client is NOT called (spy); audit client raising → 200 with `trail: []` (graceful); the real `AuditInvokeClient` builds URL `.../v1.0/invoke/audit-svc/method/trail/{cid}` (assert via httpx MockTransport). Existing intake status tests must pass unmodified.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN + gates** (this touches merged intake — its suite stays green except the explicitly-added test file).
- [ ] **Step 5: Commit** — `feat(intake): ?trail=true composes the audit trail via Dapr service invocation (M5 sync leg)`

---

### Task 4: Notification service — vendor HW3 domain + FastAPI/afcommon adapter (D-006)

**Files:**
- Create: `services/notification/` — VENDOR (copy) the domain modules from `task3/zionnet-task3/notifications-api-python/src/`: `models.py`, `segmenter.py`, `storage.py`, `processor.py`, `providers/` (email/sms/push, mocked). Adapt imports to package-relative. NEW: `src/main.py` (FastAPI, afcommon logging `notification-svc`, healthz, mount routers), `src/subscriptions.py` (Dapr subscriber), `src/deps.py`, `requirements.txt` (FastAPI + afcommon, DROP Flask), `Dockerfile`.
- Also vendor the tests that still apply: `test_segmenter.py` (pure logic, keep as-is with import fixups) — the reused test suite coming along is part of D-006's value.
- Test: `services/notification/tests/test_healthz.py`, `test_subscriptions.py`, `test_segmenter.py` (vendored).

**Interfaces:**
- `GET /dapr/subscribe` → `decision-made`, `payment-completed`, `payment-failed` → `/events/{topic}`.
- Handlers: `parse_cloudevent` → `bind_event_context` → dedupe → map event to a submitter-facing notification (plain-language message from the event's `reasoning`/`reason`; channel defaults to email using the invoice submitter if available, else a placeholder) → `storage.add_notification(...)` → `processor.send_one(...)` (mock providers "deliver") → ack. Only NOTIFY on: decision-made where route ∈ {human_review, reject} (submitter should hear it paused/was rejected), payment-completed, payment-failed. auto_approve+pending decisions are not independently notified (the terminal payment event covers the outcome) — document the choice.
- Seeding DISABLED (no `seed()` at import). Storage in-memory (documented exception). Keep the vendored `processor`/`segmenter`/`providers` logic intact — that's the reused, previously-graded code.
- `deps.py`: overridable storage + processor seams for tests.

- [ ] **Step 1: Failing tests:** subscribe lists the three topics; a payment-completed event → a notification is created + "sent" (status sent, provider invoked — spy); a decision-made human_review → notification; a decision-made auto_approve → NO notification (documented filter); redelivered event → one notification (dedupe); vendored `test_segmenter` passes unchanged (SMS segmentation logic reused). Healthz.
- [ ] **Step 2: RED.** **Step 3: Implement** (vendor + adapt). **Step 4: GREEN + gates** (no `from libs.`/`from services.` absolute imports).
- [ ] **Step 5: Commit** — `feat(notification): vendor HW3 domain behind FastAPI+afcommon adapter with dapr subscriber (D-006)`

---

### Task 5: Compose wiring + smoke (Postgres CAS characterization, full trail, F10, notification)

**Files:**
- Modify: `docker-compose.yml` (add `audit-svc` port 8005 + sidecar with BOTH statestore components mounted, `notification-svc` port 8006 + sidecar; audit depends_on postgres healthy), `scripts/smoke-compose.sh`

**Smoke additions:**
1. **Postgres audit-store CAS characterization** (like the Redis ETag block): exec inside audit-svc, exercise `DaprStateStore(store_name="statestore-audit")`: first-write, read etag, fresh-etag save, STALE-etag save (record the status/body — does the postgres component return 409? 500? characterize it), first-write-only on existing key. If observed behavior contradicts `DaprStateStore.try_save`'s handling, FIX afcommon (as Phase 01 did for Redis) and report. **This is the D-017 gate — the audit append path must not be trusted until this passes.**
2. **Full trail (F9):** pick a completed journey's correlation id (journey A's INV-1001). After it reaches `paid`, GET `http://localhost:8005/trail/{cid}` → assert it contains invoice-submitted, decision-made, payment-completed entries (the full chain).
3. **`?trail=true` (M5):** `GET http://localhost:8001/api/invoices/{TRACKING}?trail=true` → response has a non-empty `trail` array (intake→audit via Dapr invoke worked).
4. **F10:** `GET http://localhost:8005/audit/ceiling-compliance` → `violations: []` and `autoApprovalsChecked >= 1` (INV-1001 auto-approved in-ceiling; the empty violations list IS the proof).
5. **Notification:** after journey A/D, `GET http://localhost:8006/notifications` → at least one notification with status `sent` for the outcome.
- [ ] Run the FULL smoke from cold (`docker compose down -v` — now also wipes postgres-data). Space queue polls as established. Capture transcript. Diagnose real failures from sidecar logs.
- [ ] Full gates. Commit — `feat(audit,notification): compose wiring + smoke (Postgres CAS, F9 trail, F10, M5 invoke, notify)`

---

### Task 6: PR, CI, docs

- [ ] README Try-it: trail endpoint + `?trail=true` + ceiling-compliance + notifications; roadmap tick Phase 06. Update the system-map/service-relations note if audit's Postgres store is worth showing (optional).
- [ ] Commit `docs: audit + notification usage; roadmap tick for phase 06`.
- [ ] (Controller) push, PR "Phase 06 — Audit + Notification", CI green, merge.

---

## Self-Review (done)

- **Spec coverage:** F9 (Task 1 trail + Task 2 recording), F10 (ceiling-compliance index/endpoint — empty-violations-is-the-proof), M5 synchronous service invocation (Task 3 — the ONE sync service-to-service call, previously only pub/sub), D-006 (Task 4 genuine domain reuse + adapter), D-017 (Postgres component + Task 5 characterization gate), M14 (afcommon logging both services), M10 (dedupe both).
- **Carry honored:** payment terminal-event-drop → audit append-only + tolerates gaps, never assumes exactly-once (documented in Task 2).
- **Placeholder scan:** the F10 "how do you query a KV store" gap was resolved at planning time (secondary index projection) rather than left for the implementer.
- **Type consistency:** TrailEntry/AuditTrail names consistent across Tasks 1–2; audit_client URL shape matches Dapr invoke API; notification vendored modules keep their original names.
- **Risks:** (1) Postgres Dapr component CAS semantics unknown until Task 5 — same characterization discipline that de-risked Redis in Phase 01; the append path is a CAS loop, so a well-behaved 409-on-conflict component works unchanged. (2) Vendoring Flask-era code into a FastAPI service — keep the domain modules (processor/segmenter/providers/storage/models) intact and only replace the Flask `main.py` transport; the reused code is the graded part. (3) `?trail=true` touches merged intake — additive, best-effort, byte-identical without the flag.
