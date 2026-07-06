# ApprovalFlow — System Design

**Date:** 2026-07-03 · **Status:** Approved section-by-section in brainstorming; pending final review
**Deadline:** `main` frozen EOD **2026-07-12** · **Companion:** `decisions.md` (D-001…D-014, full trade-offs)

ApprovalFlow is a microservice, AI-assisted invoice/expense approval platform (course capstone).
It ingests invoices asynchronously, has an AI agent judge them against `policy.md`, auto-approves
the low-risk majority through a **deterministic router** (the agent only recommends), escalates the
rest to a human whose pause/resume is durable, pays approved items through a compensating saga,
and makes every decision auditable via one correlation id.

---

## 1. Scope

**In scope for `main` (by 12/07):** all must-haves M1–M18, all functional stories F1–F10, the four
acceptance journeys + anti-cheese guards (D5), and — strictly in this order, only as time allows —
N6 (multi-layer tests), N1 (JWT roles), N5 (RAG), B1 (eval harness).

**Out of scope / `dev` branch:** MAF agent adapter, Dapr Workflow saga variant, MCP server (B2),
Kubernetes (B3), CD publish (N2), OTel traces (N4), outbox/bulkhead (N3) — unless everything else
is green early. **Acknowledged trade:** we defer three "expected" nice-to-haves (N2/N3/N4) while
taking bonus B1, because N6+B1 share the D5 verification machinery (near-zero marginal cost) and
B1 is the evidence base for the graded dilemma criterion. N4 (OTel) is the deferral most worth
revisiting if time allows — it reinforces F9's end-to-end trail; until then, correlation-id
structured logs provide that trail.

**Stack (D-001, D-004, D-008, D-009):** Python + FastAPI services · OpenRouter LLM (stub in CI) ·
Nginx gateway · Vite+React UI · Redis backing both Dapr state and pub/sub.

## 2. Architecture

Six app services, each a small FastAPI app with a Dapr sidecar and its own Dockerfile:

| Service | Owns | External surface |
|---|---|---|
| **intake-api** | invoice record + status state machine, tracking ids, dashboard counters (F8) | `POST /invoices` → 202 + tracking id · `GET /invoices/{id}` · `PUT /invoices/{id}` (resubmit after send-back) · `GET /dashboard` |
| **decision-svc** | `DecisionAgent` port + adapters, deterministic validators, **the router**, thresholds config, vendor trust history | subscribes `invoice.submitted` · `PUT /config/thresholds` (F7/M13) |
| **approval-svc** | escalation queue records, HITL pause/resume | `GET /approvals/queue` (F4) · `POST /approvals/{id}/verdict` (F5) |
| **payment-svc** | budgets, reservations, `PaymentSaga`, mock `PaymentProvider` | subscribes decision/approval events |
| **audit-svc** | immutable per-correlation-id decision trail (F9/F10) | `GET /trail/{correlationId}` |
| **notification-svc** | vendored task3 service (D-006): email/SMS/push (mocked), retry logic | existing REST API + new Dapr subscriber endpoint |

**Infra containers:** Nginx (single entry point: rate-limits `/api/*`, routes to services, serves the
built UI statics — M6/M7), Redis, Dapr sidecars + placement. One `docker compose up` brings up
everything (M4), health-check-gated startup ordering.

**Monorepo layout (M1):**

```
services/<name>/{src,tests,Dockerfile}   # six self-contained services
libs/afcommon/                           # ONLY shared code: JSON logging middleware,
                                         # event envelope models, CAS/idempotency helpers
ui/                                      # Vite + React SPA
gateway/nginx.conf
dapr/components/                         # statestore, pubsub, secretstore YAML
docs/{ARCHITECTURE.md, adr/, PRODUCT-DILEMMA.md, superpowers/specs/}
verification/                            # D5 journey runner (make verify)
policy.md · sample-invoices.json · decisions.md · docker-compose.yml
.github/workflows/ci.yml · .env.example · LICENSE · README.md
```

## 3. Events & invoice state machine

Five pub/sub events (CloudEvents via Dapr on Redis). Every payload carries `invoiceId`,
`correlationId` (minted once at intake, on every event and log line — M14/F9), and `eventId`
(consumer-side dedupe — M10, at-least-once delivery).

| Event | Publisher → consumers | Key payload |
|---|---|---|
| `invoice.submitted` | intake → decision, audit | full invoice |
| `decision.made` | decision → approval \| payment, intake, audit, notification | `route`, recommendation, confidence, violations[], plain-language reasoning, `usdAmount`, `ceilingAtDecision` |
| `approval.resolved` | approval → payment, intake, audit, notification | verdict (approved/rejected/needs_info), approverId, comment |
| `payment.completed` | payment → intake, audit, notification, decision (trust history) | amount, budget after |
| `payment.failed` | payment → intake, audit, notification | reason, `compensated: true` |

**Status state machine (owned by intake, driven only by events):**

```
submitted → evaluating → { rejected | duplicate                     (terminal)
                         | auto_approve ────────────┐
                         | human_review → pending_approval → approved ┐
                                           ├→ rejected                │
                                           └→ needs_info ─resubmit─→ evaluating
                                                                      ▼
                                                     paying → paid | payment_failed }
```

**Idempotency — three layers (M10):**
1. *Duplicate submissions* (F3): fingerprint `sha256(vendor+invoiceNumber+total)`, checked as
   **router gate #1** (D-011) with atomic ETag check-and-register — uniform `decision.made` flow,
   no second agent call, handles concurrent identical submissions. Fingerprint stores owning
   `invoiceId`, so a send-back resubmission is not its own duplicate.
2. *Redelivered events*: every consumer records processed `eventId`s and skips repeats — one
   shared implementation in afcommon (`EventDedupe`, D-016); dedupe state stays per-service via
   Dapr's app-id key prefix (D-015).
3. *Retried payments*: saga record + provider idempotency key = `invoiceId`.

## 4. Decision service

**Pipeline per `invoice.submitted`:**
`[Gate 1: GLOBAL-DUP] → [Gate 2: deterministic validators] → [agent call] → [Gate 3: router] → decision.made`

- **Gate 2 (pure Python, before/independent of the agent):** FX→USD conversion (GLOBAL-FX; once,
  rides on the event; money = integer cents/Decimal; rates come from config state, seeded from
  `sample-invoices.json → fxRates` and editable like the thresholds — static rates as the
  documented demo simplification of "submission-date rate"), math reconcile (GLOBAL-MATH), receipt
  (GLOBAL-RECEIPT), known vendor (GLOBAL-VENDOR), missing info (MEAL-01/MEAL-02), and
  **TRAVEL-02** (single travel expense > $1,500 — a pure amount check; INV-1019 expects its tag)
  → hard-stop flags. Class-of-travel (TRAVEL-03) stays agent-side — it's semantic, not arithmetic.
- **Agent (the only LLM step)** — `DecisionAgent` port (D-003): invoice + policy rules in →
  structured `AgentRecommendation{recommendation, confidence, policy_violations[], fraud_signals[],
  reasoning}` out. Adapters: `handrolled` (OpenRouter, default) · `stub` (CI/tests) · optional
  `maf` later (D-003's three-adapter cap). A `malicious_stub` test double (always approve @ 1.0)
  lives in decision-svc's test suite only, for the M12 proof. Port speaks domain language only. The agent contributes what
  code can't: semantic category checks (alcohol-only → MEAL-03, class-of-travel), fraud judgment,
  ambiguity → low confidence, plain-language reasoning (F2/F4).
- **Gate 3 — the router, a pure function (~40 lines):**
  `duplicate` if gate 1 · `reject` if reject-severity violation (e.g. MEAL-03) · `human_review` if
  any hard stop ∨ fraud signal ∨ `usd > effectiveCeiling` ∨ `confidence < 0.80` ∨ category
  policy violation (e.g. SAAS-01) · else `auto_approve`.
  The agent can lower an outcome, structurally never raise it past a guard.
  **F2 on agent-skipping paths:** routes decided without the LLM (duplicates, deterministic
  hard-stops when the agent is unavailable) get a router-synthesized plain-language reason from
  templates (e.g. "Already processed as INV-1001 — a submission is never paid twice.") — every
  `decision.made` carries a human-readable `reasoning`, whatever produced it.

**Autonomy posture (D-012, → PRODUCT-DILEMMA.md):** `effectiveCeiling = min($250 global,
category cap)`, raised to **$400** iff vendor+category has ≥1 previously *successfully paid* item
(trust history owned by decision-svc from `payment.completed`). Category caps: SaaS ≤ $200/mo,
meals ≤ $75/attendee. Zero shipped fixture labels change (verified fixture-by-fixture; INV-1013's
vendor has no history, anti-cheese intact). We add ≥2 of our own fixtures proving the uplift
(repeat vendor at $320: first sight escalates, recurring auto-approves).

**M12 proof, four layers:** (1) adversarial `malicious_stub` (always approve @ 1.0) over all
fixtures — every non-auto label must still hold; (2) property/boundary unit tests on the router
(ceiling ± 1 cent, threshold edges, each hard stop); (3) audit evidence — every `decision.made`
records `usdAmount` + `ceilingAtDecision` for the F10 query; (4) payment-svc independently
re-checks the ceiling before paying (§6).

**M13/F7:** thresholds/caps/tier config in Dapr state, hot-edited via `PUT /config/thresholds`,
read per evaluation; `policy.md` volume-mounted.

**LLM failure (M15):** timeout/429/5xx → 3 retries w/ exponential backoff → escalate as
`human_review` with violation `AGENT-UNAVAILABLE` and honest reasoning. Degrades to all-human;
never silent, never blind approval.

## 5. Approval service (M11, F4, F5)

**The pause is a record, not a process** (D-013): on `route=human_review`, write escalation record
(agent rationale included) to Dapr state + add to `queue:pending` index (ETag CAS). Service can
crash/restart freely — nothing waits in memory. `GET /approvals/queue` reads records (F4).
`POST /approvals/{id}/verdict` applies approve/reject/needs_info **only if `status == pending`**
(CAS — double-clicks have one effect), records who/when (F9), publishes `approval.resolved`.
Send-back: intake flips to `needs_info`; submitter resubmits via `PUT /invoices/{id}` → re-enters
`evaluating`, same invoice + correlation id.

**M11 proof (journey B):** submit INV-1003 → `pending_approval` → `docker compose restart
approval-svc` → queue intact → verdict → paid.

## 6. Payment service (M9, M10, M12)

`PaymentSaga` with persisted step-marker `saga:{invoiceId}`:
`started → reserved → paid | compensated | rejected_insufficient_budget` (D-014).

- **Guards:** terminal saga record ⇒ redelivered event, skip. For `auto_approve` routes: re-verify
  `usd ≤ effectiveCeiling` independently (M12 layer 4) — refuse and flag otherwise.
- **Seeding:** payment-svc seeds `budget:{department}` state at startup from the `budgets` block of
  the mounted `sample-invoices.json` — idempotently (only if the key doesn't exist), so restarts
  never reset spent budgets.
- **Reserve = ETag CAS loop** on `budget:{department}`: read remaining → check → conditional write →
  on conflict re-read/re-check/retry; insufficient → `rejected_insufficient_budget` →
  `payment.failed(insufficient_budget)`. Proves INV-1014A/B: two $600 items vs $1,000 — exactly one
  pays, budget never negative, no locks.
- **Execute:** mock `PaymentProvider`; **deterministic failure injection** — fails iff
  `FAILURE_INJECTION_ENABLED=true` (set only in compose/test environments, default off) **and** the
  invoice carries `scenario: "payment-failure:*"` (exactly how INV-1012 ships). Double-gated so no
  production payload could ever self-inflict a failure; the field can only break a payment, never
  approve one.
- **Compensate on failure:** CAS the reservation back, mark `compensated`,
  `payment.failed{compensated: true}` — no orphaned reservation.
- **Crash between reserve and pay:** redelivery reads `reserved`, checks provider idempotency
  record, resumes or compensates.

## 7. Audit & notification

**audit-svc:** subscribes all five events, appends `{event, timestamp, correlationId, payload}` to
an immutable per-correlation-id trail. `GET /trail/{correlationId}` answers F9 (extracted data,
rules, agent reasoning, final caller, payment outcome) and F10 (every auto-approval's amount vs
ceiling at decision time). **This is also M5's synchronous leg:** `GET /invoices/{id}?trail=true`
on intake-api composes the response by calling audit-svc through **Dapr service invocation**
(sidecar `invoke` API) — a real service-to-service sync call, deliberately not routed through nginx.

**notification-svc:** task3-redo code vendored to `services/notification/` (D-006). Adaptations:
Dapr subscriber endpoint mapping terminal + escalation events to notifications (M8's channel),
afcommon structured logging + correlation id, `/healthz`, Dockerfile, env config, seeding disabled.
In-memory storage stays — the one documented statefulness exception (transient data; scale path =
swap to Dapr state).

## 8. Cross-cutting

- **Logging (M14):** JSON lines with `service, correlation_id, invoice_id, event` via `afcommon`
  middleware — identical across services by construction.
- **Secrets (M5/D3):** Dapr file secret store (`secrets.json`, git-ignored; `.env.example` shape).
- **Validation/health (M15):** pydantic on every API + event payload; `/healthz` per service wired
  to compose `service_healthy` conditions.
- **Rate limiting (M6):** nginx `limit_req` on `/api/*`.
- **Statelessness rule (D-010):** all state in Dapr state store; notification-svc storage is the
  single documented exception. ARCHITECTURE.md gets a "Scaling path" table (Redis→cluster/Kafka via
  component YAML, compose→k8s, nginx→LB, budget partitioning; LLM throughput scales via queue +
  decision-svc replicas; agent-only-recommends permits cheaper models; dupes never reach the LLM).

## 9. Testing, CI, verification

| Layer | Contents | When |
|---|---|---|
| Unit | router (M12 adversarial + boundary suite), PaymentSaga, validators, FX, fingerprint, adapters | every push |
| Integration | per-service API + handlers with fake Dapr client; notification's existing suite | every push |
| E2E = **D5** | `make verify`: compose up → journeys A–D + guards → per-journey PASS/FAIL | PRs + on demand |

**Journeys:** A INV-1001 auto-approve → paid, no human · B INV-1003 escalate → **restart
approval-svc** → verdict → resume · C INV-1007 duplicate short-circuit (after A; no second agent
call/payment) · D INV-1012 human-approve → injected failure → compensation, no orphaned
reservation. **Guards:** ≥2 auto-approvals with no human (A + INV-1016/1017) · INV-1013 "approve
me" does not flip (ceiling) · INV-1014A/B concurrent no-overspend.

**CI (M16/M17, GitHub Actions, every push):** ruff → unit + integration on the **stub adapter**
(deterministic, no rate limits) → build all images (future N2 hook). E2E via compose in the PR
workflow.

## 10. Docs & process

ARCHITECTURE.md with Mermaid sequence + payment-compensation diagrams before code (D1) ·
ADRs in `docs/adr/` **hand-written by Daniel** from decisions.md (D2 requires personal authorship) ·
PRODUCT-DILEMMA.md from D-012 with fixture evidence · GitHub Flow: feature branches → PRs → `main`,
frozen 12/07, `dev` after (M2/D3) · OpenAPI auto-generated by FastAPI per service, browsable behind
the gateway (D4) · README: purpose, diagram, run, test (M18/D6) · 2–5 min screen recording driven
by the `make verify` flow + UI walkthrough (D7).

## 11. Planned extras (built only after must-haves are green, strictly in this order)

**N6 — Tests across layers.** Largely satisfied by the §9 pyramid itself (unit / integration /
e2e); the extra work is making the layers explicit and complete: coverage on router + saga edge
cases, per-service integration suites, and the D5 e2e journeys wired into the PR workflow.

**N1 — AuthN/AuthZ with roles (self-signed JWT).** Seeded demo users (submitter / approver /
admin). `POST /api/auth/login` (hosted in intake-api) issues a self-signed JWT with a `role`
claim; a shared `afcommon` FastAPI dependency validates the token and enforces roles per endpoint:
submitter → submit/status, approver → queue/verdict, admin → `PUT /config/thresholds`. Nginx
passes `Authorization` through untouched; the React UI gets a login/role picker and sends the
bearer token.

**N5 — RAG over the policy.** At decision-svc startup, `policy.md` is chunked **one chunk per
`rule_id`** (the file is written for this). Retrieval is lightweight and fully local — category
filter (a meals invoice always gets MEAL-* + GLOBAL-*) plus BM25 keyword scoring over line-item
descriptions and notes — no embedding model, no new heavy container. Only the top-k relevant rules
enter the agent prompt; retrieved `rule_id`s are logged onto the decision event for the audit
trail. Falls back to full-policy-in-prompt via config flag (`RAG_ENABLED=false`).

**B1 — Automated eval harness.** Reuses the D5 machinery: runs the full labeled set (20 shipped
fixtures + our added hard cases: trust-tier pair, adversarial variants) through the decision
pipeline, compares `expected.route` vs actual, and writes a committed markdown report
(`eval/REPORT.md`): per-route accuracy, confusion matrix, per-fixture table, and the
malicious-stub safety sweep. CI runs it with the stub adapter; a manual run against the real
OpenRouter model produces the committed report — the evidence base for PRODUCT-DILEMMA.md.

## 12. Requirements traceability

| Req | Where satisfied |
|---|---|
| F1 | intake `POST /invoices` returns 202 + tracking id immediately; all processing async behind pub/sub |
| F2 | `GET /invoices/{id}`: status + agent's plain-language reasoning |
| M1 | single private GitHub monorepo, layout in §2 — everything needed to run/test lives in the repo (`git init` of `final-project/` at implementation start) |
| M2 | `main` = everything evaluated, frozen EOD 12/07; further work only on `dev` branched off it (§10) |
| M8 | async intake (202 + tracking id) + final result delivered via notification-svc (§7), driven by terminal events |
| F3/M10 | §3 idempotency layers 1–3 |
| F4/F5/M11 | §5 approval-svc; restart test |
| F6 | D-012 posture: category caps + trust uplift keep autonomy meaningful |
| F7/M13 | thresholds in Dapr state + `PUT /config/thresholds`; policy volume-mounted |
| F8 | intake dashboard counters (auto vs human rates, amounts) |
| F9/F10 | audit-svc trail; `ceilingAtDecision` on every decision |
| M3/M4 | 6 services, one compose up |
| M5 | Dapr pub/sub (all events) · **sync service invocation: intake-api → audit-svc** for `GET /invoices/{id}?trail=true` (§7) · state (everywhere) · secrets (LLM key) |
| M6/M7 | nginx rate-limited single entry; React UI |
| M9 | §6 saga + compensation |
| M12 | §4 four-layer proof |
| M14/M15 | §8 afcommon logging; validation, health, fail-clean LLM path, provider ACL (D-003) |
| M16/M17 | §9 CI |
| M18 | §10 README |
| N6 | §9 test pyramid made explicit per layer (§11) |
| N1 | self-signed JWT + role enforcement via afcommon dependency (§11) |
| N5 | per-rule chunking + local BM25 retrieval in decision-svc (§11) |
| B1 | eval harness over labeled fixtures → committed `eval/REPORT.md` (§11) |

## 13. Risks

1. **OpenRouter rate limits during demo** → retry/backoff, Groq as second configured provider, stub as last resort (D-004).
2. **Trust-tier scope creep** → graceful degradation pre-agreed: ship category caps only, tier documented as designed (D-012).
3. **Compose/sidecar startup flakiness in CI e2e** → health-gated `depends_on`, generous waits, e2e on PRs only (pushes run unit+integration).
4. **Fixture label drift** — any threshold change requires per-fixture audit; D-012 chosen precisely to avoid relabeling.
