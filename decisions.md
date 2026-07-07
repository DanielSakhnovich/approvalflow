# ApprovalFlow — Decision Log

> Living log of every significant decision made during design and implementation.
> Each entry: what we chose, what the alternatives were, and the advantages/disadvantages
> that drove the choice. New decisions get appended; reversed decisions get a
> **Superseded by** note — never deleted.
>
> ⚠️ **Relation to D2 (ADRs):** the assignment requires short ADRs *written personally by Daniel,
> not by an AI agent* — those live separately in `docs/adr/`. This file is the raw working log
> that feeds them; rewrite in your own words before submission.

---

## D-001 — Implementation language: Python

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** All services are written in Python (FastAPI).

**Alternatives considered:** C#/.NET · TypeScript/Node · polyglot mix.

**Advantages of the choice:**
- Daniel's proven stack from prior course work (notification-service HW3, MAF medical-triage task2) — fastest path under a 9-day deadline.
- First-class Dapr SDK; FastAPI auto-generates the OpenAPI spec required by D4.
- Enables direct reuse of the existing notification service (see D-006).

**Disadvantages accepted:**
- C# would have aligned with Alon's (CTO/instructor) home stack and has the most mature Dapr support.
- Polyglot would have shown range; single language shows less breadth but multiplies nothing (CI, tooling, debugging stay simple).

---

## D-002 — Scope posture: all must-haves + prioritized extras

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Lock all M1–M18 + the four acceptance journeys first. Then add extras in priority order: **N6 (multi-layer tests) → N1 (JWT roles) → N5 (RAG) → B1 (eval harness)**. Anything that doesn't fit moves to the post-deadline `dev` branch.

**Alternatives considered:** must-haves only · everything including all bonuses on `main`.

**Advantages of the choice:**
- Matches the evaluation rubric: must-haves are expected, optionals rated good/excellent — this captures optional credit without gambling the required core.
- The chosen extras are the cheapest per point: N6/B1 build on the D5 verification work we must do anyway; N1 attaches at the gateway; N5 is contained inside decision-svc.

**Disadvantages accepted:**
- Must-haves-only would be safer but leaves expected points on the table.
- Going for all bonuses (MCP server, k8s, CD) risks eating days that the graded journeys need.

---

## D-003 — Agent technology: hand-rolled loop behind a `DecisionAgent` port

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** The agent is a hand-rolled tool-calling loop with structured output, hidden behind a
`DecisionAgent` interface (port) that we own. Adapters: `handrolled` (default), `stub` (CI/tests),
`maf` (optional, only post-green or on `dev`).

**Discipline rules agreed:**
1. One port, three adapters max.
2. The port speaks domain language only (invoice in → recommendation out); no provider/message/token types in the signature.
3. No port-and-adapter pattern anywhere else — only where volatility is real (the LLM). Dapr already abstracts state/pubsub/secrets.
4. The MAF adapter is built only if everything else is green, or on `dev`.

**Alternatives considered:** MAF natively · LangGraph · plain hand-rolled without the named pattern.

**Advantages of the choice:**
- ~95% of it is mandatory work anyway: the assignment mandates a stubbed model in CI and M15 demands a config-swappable provider — both force an interface with multiple implementations. This decision just names the pattern (ACL/port-and-adapters) and collects the credit.
- Total debugging control — no framework internals under a 9-day clock.
- Trivially demonstrable M15 (one interface, adapters picked by one env var) and a ready-made ADR (agent = volatile component ⇒ encapsulate, straight from Alon's volatility lecture).
- Keeps a free option on showcasing MAF later without committing to it.

**Disadvantages accepted:**
- We write retry/timeout/structured-output-parsing plumbing ourselves (modest: Pydantic + OpenAI-compatible JSON mode).
- Showcases the course framework (MAF) only as a footnote; "we built on MAF" would have been a legitimate story for the AI course.
- Any abstraction adds indirection; a badly designed port could leak one implementation's shape (mitigated by rule 2).

**Compliance note:** no agent-tech option compromised any requirement — M12 is enforced by the
deterministic router outside the agent, and M11 pauses at the workflow level, not inside the agent loop.

---

## D-004 — LLM provider: OpenRouter (free tier), stub in CI

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Live runs use OpenRouter (OpenAI-compatible API, model chosen by config). CI and the
verification command always use the `stub` adapter.

**Alternatives considered:** Groq · Gemini free tier · local model via Ollama.

**Advantages of the choice:**
- Existing key + proven from task2; model switching is a config string; OpenAI-compatible shape keeps the hand-rolled client simple.
- Stub in CI satisfies the assignment's "stubbed/local model for CI/eval" constraint and removes rate limits from the critical path.

**Disadvantages accepted:**
- Free-tier rate limits could bite during demo recording (mitigations: retry/backoff, swap-by-config to Groq as a second provider, stub as last resort).
- Ollama would remove rate limits entirely but small local models reason worse over policy and bloat compose; Gemini's native API shape means extra adapter work.

---

## D-005 — Service decomposition: 6 services

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Six app services — **intake-api, decision-svc, approval-svc, payment-svc, audit-svc,
notification-svc** — plus Nginx gateway, React UI, and Redis as infra.

**Alternatives considered:** 3 services (minimum) · 4 services (recommended by Claude) · 5 (4 + audit).

**Advantages of the choice:**
- Purest bounded contexts; each service states its purpose in one sentence.
- A dedicated immutable **audit-svc** is a strong compliance story for this product (F9/F10): the audit log has different consistency/retention/access needs than operational state.
- **notification-svc** is justified by reuse (D-006) — the cost objection ("50 lines of logic wearing a full microservice costume") falls away when the service already exists.
- More impressive decomposition to present.

**Disadvantages accepted:**
- Two more Dockerfiles, sidecars, health checks, CI targets, and compose entries than the 4-service option; larger distributed-debugging surface in the final days.
- Service count itself is not graded (M3 floor is 3); the marginal value is story, not points.
- Claude's recommendation was 4 services on cost/benefit grounds; Daniel chose 6 — the reuse argument (D-006) tipped notification, and the compliance story tipped audit.

---

## D-006 — Reuse Daniel's existing notification service (task3 redo version)

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Vendor (copy) the HW3-redo notification service (`task3/zionnet-task3/notifications-api-python`)
into the monorepo as `services/notification/`.

**Alternatives considered:** the original screening version · building a fresh minimal notifier.

**Advantages of the choice:**
- The redo is the hardened version: tests included (helps N6 immediately), channel validation, retry cap, processing guard.
- Real reuse of owned code — less work than fresh, and a nice narrative (screening task graduates into the capstone).

**Disadvantages accepted / adaptation debt:**
- Must be adapted: add a Dapr pub/sub subscriber endpoint (M5/M8), structured logging + correlation id (M14), health check (M15), Dockerfile, env config, disable sample-data seeding.
- In-memory storage stays (acceptable: notifications are transient) — documented as the one stateful-in-process exception with a "swap to Dapr state for multi-replica" scale path (see D-010).

---

## D-007 — Coordination: event choreography + explicit local saga in Payment

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Services coordinate by reacting to each other's pub/sub events (choreography); each
persists its own state machine in Dapr state. The money-critical sequence (reserve budget → execute
payment → compensate on failure) is an explicit, unit-testable **`PaymentSaga` class inside
payment-svc**, which owns all money state. HITL pause = a durable escalation record in Dapr state;
resume = verdict API publishes an event.

**Alternatives considered:** Dapr Workflow (central durable orchestrator) · pure choreography
(compensation also event-driven).

**Advantages of the choice:**
- Every graded proof is concentrated and testable: compensation in one class (M9), pause/resume as a state record whose restart-survival is a 5-line integration test (M11), no-overspend via one concurrency point (INV-1014A/B).
- Debugging is plain-Python debugging + per-correlation-id logs — no replay archaeology in the final days.
- Uses only the Dapr primitives the assignment already requires (pub/sub, state); zero new runtime concepts.
- Loose coupling between services; audit-svc attaches as just another subscriber.

**Disadvantages accepted:**
- No single place in code shows the end-to-end flow (mitigated: audit timeline reconstructs it per invoice; ARCHITECTURE.md sequence diagram documents it).
- We hand-write durability boilerplate (idempotent handlers, status transitions) that Dapr Workflow would have provided — accepted to avoid workflow determinism/replay bugs surfacing under crash tests late in the game.
- Dapr Workflow would have been the deeper Dapr showcase; noted as a possible `dev`-branch swap for the saga (same "safe core, showcase later" shape as D-003).
- Pure choreography was rejected: it scatters the M9/F10 consistency proofs across event-handler chains — maximal decoupling of exactly the thing (payment internals) that has a single natural owner.

---

## D-008 — API gateway: Nginx

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Nginx is the single external entry point: routes `/api/*` to services, `limit_req`
for rate limiting (M6), and serves the built UI's static files.

**Alternatives considered:** Traefik · custom FastAPI gateway.

**Advantages of the choice:**
- Config-only — zero gateway code to write, test, or defend; one container covers routing + rate limit + static UI.
- Battle-tested and instantly recognizable to graders.

**Disadvantages accepted:**
- N1 (JWT) needs a different insertion point than a custom Python gateway would offer (plan: validate JWTs in services via shared middleware, or nginx `auth_request` if needed).
- Traefik's dynamic config and dashboard were nice-to-haves, not needs.

---

## D-009 — UI: Vite + React SPA · Infra: Redis for both state and pub/sub

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** The minimal UI (M7) is a Vite + React SPA (submit form, status timeline, approver
queue with agent rationale, dashboard) built once and served as statics by Nginx. A single Redis
container backs both the Dapr state store and the pub/sub broker.

**Alternatives considered (UI):** plain HTML/vanilla JS · Streamlit.
**Alternatives considered (infra):** RabbitMQ + Redis · RabbitMQ + Postgres.

**Advantages of the choice:**
- React: clean demo-friendly SPA, fast to generate, no extra runtime container (static files).
- Redis-only: smallest compose file, fewest startup-ordering failure modes, the standard Dapr default; because Dapr abstracts the component, swapping to RabbitMQ/Kafka/Postgres later is a YAML edit, not a code change.

**Disadvantages accepted:**
- Vanilla HTML would ship faster but demo weaker; Streamlit is fast for dashboards but an odd fit for a submit/approve product and adds a Python web runtime.
- A "real" broker (RabbitMQ) would look more enterprise-grade; rejected as one more container to health-check for zero functional difference behind Dapr.

---

## D-010 — Design rule: stateless services (the "millions of users" answer)

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** No service holds meaningful in-process state; all state lives in the Dapr state store.
One documented exception: notification-svc's in-memory storage (transient data; scale path = swap to
Dapr state). ARCHITECTURE.md gets a "Scaling path" section with the demo-vs-scale swap table.

**Context:** the assignment says "assume it serves millions of users." The architecture must make
scale a deployment/config concern, never a redesign.

**Advantages of the choice:**
- Horizontal scaling becomes trivial (1 replica ≡ 50); pub/sub lets each stage scale independently (decision-svc workers scale with LLM latency, the true bottleneck).
- Async intake (202 + tracking id) absorbs bursts; idempotency (M10) makes at-scale retries/duplicates safe; per-entity keying (invoice id / correlation id) partitions naturally.
- Every scaled-down stand-in is a component swap: Redis → cluster/Kafka (Dapr YAML), compose → k8s (B3 path), Nginx → managed LB.

**Disadvantages accepted:**
- Slightly more code than in-memory shortcuts (every read/write goes through Dapr state).
- We do not actually load-test or autoscale — the rubric grades scalability as a design/code-quality concern, not a benchmark.

---

## D-011 — Duplicate detection lives in the deterministic router (gate #1), not in intake

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Intake always publishes `invoice.submitted` (every submission gets 202 + tracking id
per F1, even duplicates). The deterministic router in decision-svc runs **GLOBAL-DUP as its first
gate**: an atomic check-and-register of the fingerprint `sha256(vendor + invoiceNumber + total)` in
Dapr state (first-write-wins via ETag). On a hit it routes `duplicate` **before the agent is called**
(the INV-1007 fixture requires "no second agent call, no second payment").

**Special case:** the fingerprint record stores its owning `invoiceId`; a send-back resubmission
(F5) whose fingerprint resolves to *itself* is a resubmission, not a duplicate.

**Alternative considered:** dedupe synchronously in intake before publishing (short-circuit,
never publish `invoice.submitted` for duplicates).

**Advantages of the choice:**
- **One home for all routing:** GLOBAL-DUP is a policy rule like any other; every invoice exits
  through the same `decision.made` event — uniform audit trail, notifications, and state machine.
- The atomic check-and-set in one place also resolves the race of two identical invoices submitted
  concurrently: the second loses the ETag write and routes `duplicate`.
- Strengthens the M12 story: "the deterministic router is the single place that decides routes" —
  no exceptions.

**Disadvantages accepted:**
- A duplicate costs one extra event hop before being caught (milliseconds; the expensive step —
  the agent call — is still skipped).

---

## D-012 — Dilemma posture: layered autonomy — defaults + category caps + earned trusted-vendor uplift

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Three-layer autonomy posture, enforced by the deterministic router and defended in
`docs/PRODUCT-DILEMMA.md`:

1. **Base thresholds = shipped defaults:** global ceiling **$250**, confidence **≥ 0.80**, all
   hard stops (new vendor, FX, math mismatch, fraud signals, missing receipt/info) always human.
2. **Category sub-ceilings:** an item's autonomy limit = min(global ceiling, category cap) —
   SaaS ≤ $200/mo (mirrors SAAS-01), meals ≤ $75/attendee (mirrors MEAL-01). Makes the implicit
   policy-compliance coupling explicit as autonomy structure.
3. **Trusted-recurring-vendor uplift:** if the vendor+category pair has at least one previously
   *successfully paid* item (from our own payment history in state), the per-item ceiling rises to
   **$400** for that item. Trust is earned from evidence the system itself produced; hard stops and
   confidence still apply unchanged.

**Rationale (the trade-off):** a wrong auto-approval's loss is *bounded by the ceiling* (blast
radius $250, or $400 only for vendors we've already paid safely), while a wrong escalation costs
human time and violates F6's no-rubber-stamping spirit. Confidence bounds model uncertainty;
hard stops bound categorical risk that amount can't measure; autonomy expands only with evidence.

**Fixture impact:** verified fixture-by-fixture — **zero shipped labels change** (no fixture in
the $250–$400 band has a previously-paid vendor+category; INV-1013's PixelForge has no payment
history, so the D5 anti-cheese guard survives intact). We add 1–2 of our own fixtures to prove the
uplift fires (e.g. repeat Atlassian invoice at $320: escalates on first sight, auto-approves as
recurring) — the fixture file explicitly invites expanding the set.

**Alternatives considered:**
- **Pure defaults** — zero risk/work and a defensible "autonomy is earned by data" launch story,
  but policy.md §6 explicitly says resolving the dilemma means *choosing your own*; reads as dodging.
- **Category caps only (no tier)** — zero relabeling but behaviorally ~identical to defaults;
  mostly framing. Kept as the graceful degradation: if the tier's ~half-day cost threatens the
  deadline, ship layers 1–2 and document the tier as designed future work.
- **Raise global ceiling to $500** — boldest product call, but boobytrapped: INV-1013 (the $300
  adversarial "approve me" fixture) would then legitimately auto-approve, gutting the D5
  anti-cheese demonstration, and other fixtures need re-labeling — each mislabel is a failed
  verification.

**Disadvantages accepted:**
- Vendor-history lookup enters the router path (one Dapr state read) + more router tests (~half a day).
- Uplift tier needs our own fixtures to demonstrate — new test surface we author ourselves.

**Correction (2026-07-06, Phase 03):** the trusted-uplift example must use a category without its own cap (e.g. travel) — a SaaS item stays capped at $200/mo regardless of trust, so a SaaS uplift example would be misleading. The router implements uplift as min(trusted_base, category_cap).

**M12 transparency note (2026-07-06, Phase 03):** the adversarial proof (`services/decision/tests/test_m12_adversarial.py`) shows a malicious agent cannot move money past ANY amount/ceiling/hard-stop guard — 19 of the 20 shipped fixtures keep their non-auto route even under an always-approve agent. The single exception is INV-1015 (alcohol-only, $60): rule MEAL-03 is semantic knowledge only the agent supplies, so an agent that suppresses it lets that in-budget item through. This does not weaken the ceiling guarantee — the blast radius of any such miss is bounded by the autonomy ceiling itself — but it is the honest limit of "deterministic" enforcement: category-content rules are agent-graded by design. Documented here so the M12 claim is read with its exact scope.

---

## D-016 — Consumer-dedupe wrapper extracted into afcommon

**Date:** 2026-07-06 · **Status:** Accepted (raised by Daniel; recommendation accepted)

**Decision:** The mechanical M10 consumer-dedupe wrapper — CloudEvent `data` unwrapping,
correlation/invoice contextvar binding, and the atomic `processed:{event_id}` first-time check —
lives ONCE in afcommon (`EventDedupe` + a small parse/bind helper). Intake is refactored to
delegate to it (its existing tests pin behavior); decision-svc and all later consumers (approval,
payment, audit, notification) use it from day one. Handler *bodies* (counters, publishing, sagas)
stay per-service — the helper covers only the four mechanical steps, nothing more.

**Alternative considered:** keep per-service copies of the ~10-line wrapper.

**Advantages of the choice:**
- The wrapper's internal ordering is load-bearing (dedupe-mark vs. transform-commit — already the
  subject of a documented accepted-risk analysis on intake's copy); one implementation means one
  place to reason about and one place to fix.
- Payment's dedupe guards money ("retried payments — exactly one effect"); correctness should not
  depend on copy-paste fidelity — same drift class that produced the `from libs.` Critical.
- Five concrete consumers are scheduled — this is planned duplication, not speculative abstraction;
  extraction is cheapest now (one consumer built, four unbuilt).
- Dedupe state stays per-service automatically (Dapr app-id key prefix, D-015): shared code,
  isolated data.

**Disadvantages accepted:**
- Shared code couples consumers to afcommon changes (mitigated: one monorepo, one deploy unit per
  compose build, versioned together).
- The intake refactor itself carries small regression risk (mitigated by its existing suite).

---

## D-013 — HITL pause is a durable state record, not a waiting process

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** Escalation (M11) = approval-svc writes an escalation record (invoice, agent rationale,
`status: pending`) to Dapr state + adds it to a `queue:pending` index (ETag CAS). Resume = verdict
endpoint applies approve/reject/needs_info only if `status == pending` (CAS — double-clicks and
retries have exactly one effect), then publishes `approval.resolved`. No timers, background workers,
in-memory queues, or workflow engine anywhere in the service.

**Alternatives considered:** Dapr Workflow `wait_for_external_event` (rejected in D-007) ·
in-memory queue with periodic state snapshots.

**Advantages of the choice:**
- Restart-survival is inherent: nothing waits in memory, so there is nothing a crash can kill.
  The M11 proof is a 4-step integration test (submit INV-1003 → restart approval-svc → queue
  intact → verdict → paid).
- F4 comes free: the queue endpoint reads the records, agent rationale included, no recomputation.
- Verdict idempotency (M10 for humans) falls out of the same CAS pattern used everywhere else.

**Disadvantages accepted:**
- Queue listing needs a hand-maintained index key (Dapr key-value has no native queries) — one
  more CAS write per escalation/resolution.

---

## D-014 — Payment saga mechanics: persisted progress, CAS budget reservation, deterministic failure injection, integer cents

**Date:** 2026-07-03 · **Status:** Accepted

**Decision:** The `PaymentSaga` persists a step-marker per invoice
(`started → reserved → paid | compensated | rejected_insufficient_budget`) in Dapr state:

1. **Guards before money:** redelivered-event check on the saga record (M10), and an independent
   re-check of `auto_approve ⟹ amount ≤ effective ceiling` (M12 defense-in-depth — a second
   service must also be wrong for money to move).
2. **Budget reservation = ETag compare-and-swap loop** on `budget:{department}` — concurrent
   approvals serialize on the write; the loser re-reads, re-checks, and is rejected if funds ran out.
   Proves INV-1014A/B (exactly one of two $600 items paid from a $1,000 budget, never negative).
3. **Mock PaymentProvider with deterministic failure injection:** fails iff
   `FAILURE_INJECTION_ENABLED=true` (compose/test only, default off) **and** the invoice carries
   `scenario: "payment-failure:*"` — exactly how shipped fixture INV-1012 is marked, so journey D
   self-triggers. (Double-gated after external review: harness injection, not payload steering —
   a production payload can never self-inflict a failure, and the field can only break a payment,
   never approve one.)
4. **Compensation:** release the reservation (CAS the amount back), mark `compensated`, publish
   `payment.failed{compensated: true}` — no orphaned reservations (M9).
5. **Crash between reserve and pay:** on redelivery the step-marker says `reserved`; the handler
   checks the provider's idempotency record (key = invoiceId) and resumes or compensates.
6. **Money is integer cents / Decimal end-to-end;** FX→USD conversion happens once (decision-svc
   validators) and rides on the event.
7. **Vendor payment history for D-012's trust tier is owned by decision-svc** (subscribes to
   `payment.completed`) — decision data lives with the decision service; payment stays pure money.

**Alternatives considered:** distributed locks or a single-writer actor for budgets (heavier, and
CAS is idempotent-retry-friendly) · failure injection via env var/admin endpoint (less
self-contained than the fixture's own scenario marker) · floats for money (audit-failure bait).

**Advantages of the choice:** every graded money property (M9, M10, M12, no-overspend) maps to one
small, unit-testable mechanism in one file; crash-recovery falls out of the same "progress is a
record, not a process" philosophy as D-013.

**Disadvantages accepted:**
- CAS retry loops are slightly more code than a lock; contention on one hot budget key would need
  partitioning at real scale (documented in the D-010 scale path).
- The scenario-field injection must be clearly documented so graders don't mistake it for
  payload-driven behavior.

---

## D-015 — Single-owner state keying (Dapr keyPrefix stays at appid default)

**Date:** 2026-07-04 · **Status:** Accepted

**Decision:** Leave `dapr/components/statestore.yaml` at Dapr's default `keyPrefix` behavior
(`appid`): every state key is physically namespaced per owning service (e.g.
`intake-api||fp:abc`). This is the deliberate architecture, not an unexamined default —
**single-owner keying**: every state key has exactly one owning service; cross-service data
flows via pub/sub events, never shared state keys (per docs/superpowers/specs §2/§8 service
ownership table, and D-007/D-010).

**Alternatives considered:**
- `keyPrefix: none` (global namespace) — rejected: shared keys would reintroduce hidden
  coupling and make two services silently co-own money-critical keys.
- `keyPrefix: name` — a fixed, app-name-independent prefix instead of the per-appid default;
  rejected for the same reason as `none` once more than one service targets the same logical
  component: it does not by itself enforce that a key has a single owner, it just renames the
  shared namespace.

**Advantages of the choice:**
- Enforced ownership isolation matches D-007 (choreography) and D-010 (statelessness): a
  service cannot corrupt another's state even by key collision, because the physical key
  space is partitioned per service before the app-level key is ever compared.
- No new runtime concept — it is Dapr's out-of-the-box behavior, so the "decision" is really
  "we looked at it and are keeping it," made explicit so it isn't silently relied upon.

**Disadvantages accepted:**
- A genuinely shared key (none currently exists in the design — budgets are payment-svc-only,
  fingerprints decision-svc-only, escalations approval-svc-only) would need an explicit
  component change (keyPrefix) plus a data migration if one is ever introduced.
- Flagged by the Phase 01 final review as the risk to keep visible: because the isolation is
  implicit in a default rather than enforced by code, a future contributor could add a
  cross-service read/write assuming shared keying works, and only discover the per-appid
  partition at runtime.

---

## D-017 — Persistence split: audit → Postgres; budgets stay on hardened Redis

**Date:** 2026-07-07 · **Status:** Accepted (raised by Daniel 05/07; decided 07/07)

**Decision:** The audit trail (Phase 06) gets its own Dapr state component (`statestore-audit`,
type `state.postgresql`) backed by a Postgres container. Department budgets (Phase 05) stay on
the existing Redis state store, hardened now with AOF persistence (`appendonly yes,
appendfsync everysec`) and a named volume in compose. Budgets-on-Postgres is documented as the
production path in ARCHITECTURE.md's scaling table.

**Alternatives considered:** both on Postgres · all-Redis with AOF only.

**Advantages of the choice:**
- Money CAS stays on semantics characterized against the real sidecar (Phase 01 smoke: 409 vs
  500+user_script signatures) — the INV-1014 no-overspend proof keeps its foundation days before
  the deadline; a Postgres component reports conflicts differently and would need full
  re-characterization.
- Audit gets compliance-grade durability + the polyglot-persistence story (right store per data
  shape — matches D-005's own rationale for a separate audit service), decided BEFORE audit is
  built (zero rework).
- AOF closes Redis's acknowledged-write-loss window cheaply for the demo scope.

**Disadvantages accepted:**
- One more container (postgres) + component YAML in Phase 06; audit CAS behavior on the postgres
  component must be characterized by Phase 06's smoke before the trail write path trusts it.
- Budgets' durability rests on AOF everysec (worst case ~1s of acknowledged writes on a hard
  crash) — accepted for demo scope, documented for production.
