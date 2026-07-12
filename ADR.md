# ApprovalFlow — Architecture Decision Records

## ADR-001 — Python and FastAPI for every service

**Context:** I had nine days to build this, so the language choice was really about speed. I
already know Python and FastAPI well from earlier coursework - the notification service and the
medical-triage task were both built with them. FastAPI also has a good Dapr SDK and generates
the OpenAPI spec the assignment asks for. Using more than one language would show more range,
but it would also make CI and debugging harder on every service.

**Decision:** Write all services in Python with FastAPI.

**Consequences:** This was the fastest way to get something working, and I got to reuse code I
already had. The OpenAPI spec comes for free. The downside is breadth - one language shows less
range than a mix, and C# would have matched the instructor's stack better. I picked speed.

---

## ADR-002 — Six microservices, one bounded context apiece

**Context:** The assignment asks for at least three services. The real question was how many more
to add before the extra overhead stops being worth it. Two things pushed me higher. First, the
audit log works differently from normal state - it is append-only and has its own retention and
access rules, so a separate service makes the compliance story cleaner. Second, the notification
service already existed as tested code that I own, so giving it its own service costs almost
nothing. The number of services is not graded, so this was about a cleaner design, not points.

**Decision:** Six services - intake, decision, approval, payment, audit, and notification. Each
one owns its state. The gateway and Redis are shared infrastructure.

**Consequences:** The bounded contexts came out clean, and the separate audit service reads well
for compliance. The cost is operational - more Dockerfiles, more sidecars, more CI targets, and
more places to check when a bug crosses services.

---

## ADR-003 — The agent recommends; a deterministic router decides

**Context:** The autonomy ceiling has to be something I can prove, not just claim. The provider
also has to be swappable and to fail loudly when it breaks. The LLM is the one unpredictable part
here, and the assignment already asks for a stubbed model in CI, which points to an interface with
more than one implementation behind it. The main risk I wanted to remove was the model being able
to move money on its own. I wanted that to be impossible by design, not just discouraged in the
prompt.

**Decision:** A hand-rolled agent sits behind a `DecisionAgent` port, with handrolled, stub, and
MAF adapters. Its output is only an input to a pure-function router, and the router makes the real
routing decision.

**Consequences:** The agent can only make an outcome more careful, never less, so the ceiling
holds no matter what the model says. Swapping providers is easy. In return I write my own retry,
timeout, and JSON parsing, and MAF ends up as a small footnote instead of the main story.

---

## ADR-004 — Event choreography, with an explicit saga for the money path

**Context:** The flow goes through six services, so how they coordinate is a big early decision.
There were two real options - a central orchestrator (Dapr Workflow), or choreography, where each
service reacts to events the others publish. Under the deadline, what decided it was where the
graded proofs would live. Compensation, pause and resume across a restart, and the no-overspend
rule all work best in one place I can test, not spread across event handlers or hidden inside
workflow replay logic. Money especially has one owner and should not be passed around between
events.

**Decision:** Services react to each other's pub/sub events, and each keeps its own state machine.
Payment is the exception - one `PaymentSaga` class owns all the money state.

**Consequences:** Every graded proof lands in one place with a focused test, and debugging stays
normal Python debugging. I only use the Dapr features the assignment already needs. What I lose is
a single place in the code that shows the whole flow end to end - the audit timeline and a sequence
diagram cover that instead. I also write durability code by hand that Dapr Workflow would have
given me.

---

## ADR-005 — The deterministic router owns all routing, duplicate detection included

**Context:** Every submission gets a 202 and a tracking id, so intake cannot quietly drop
duplicates up front. At the same time, a duplicate must never reach the agent (the expensive step)
or get paid twice, and two identical invoices can arrive at the same moment. So where should dedupe
go - in intake, or as another routing rule? Keeping every invoice on the same exit path (same
`decision-made` event, same audit trail, same state machine) made the routing rule the clean
answer. It also supports the claim that one place decides every route.

**Decision:** Intake always publishes `invoice-submitted`. The router's first gate is an atomic
fingerprint check-and-register (first write wins, using ETag) that routes an invoice as a
`duplicate` before the agent runs.

**Consequences:** All routing lives in one place, and the same atomic set also handles the race
when two identical invoices arrive together. The only cost is one extra event hop before a
duplicate is caught - a few milliseconds, and the agent call is skipped either way.

---

## ADR-006 — A layered autonomy posture (resolving the product dilemma)

**Context:** The policy doc leaves the autonomy dilemma to the designer, so I had to pick a clear
position and defend it. The trade-off is not even. A wrong auto-approval loses at most the ceiling
amount. A wrong escalation wastes a person's time and works against the whole no-rubber-stamping
idea, so autonomy should go as far as the evidence safely allows. But it cannot go so far that the
adversarial fixtures pass. INV-1013's "approve me" at $300 has to stay blocked, or the anti-gaming
demo stops meaning anything.

**Decision:** Auto-approve only when all of these are true - the amount is under the base ceiling of
$250 (or the category cap if that is lower, SaaS $200/month, meals $75/attendee), confidence is at
least 0.80, and there are no hard stops. The ceiling goes up to $400, but only for a vendor and
category pair I have already paid successfully.

**Consequences:** Losses stay bounded, and autonomy only grows where there is evidence for it. No
shipped fixture label changes. The cost is a vendor-history lookup on the router's path, plus a few
fixtures I write myself to show the earned-trust tier working.

---

## ADR-007 — The human-in-the-loop pause is a durable record, not a waiting process

**Context:** An escalated invoice has to survive a restart of the approval service. A person might
take hours or days to answer, so the pause outlives any running process. The easy design is
something that waits - a workflow's `wait_for_external_event`, or an in-memory queue. But anything
that waits in memory is exactly what a crash removes, which makes "prove it survives a restart" hard
to show. If I treat the pause as data instead of a process, durability comes for free, because there
is nothing waiting left to lose.

**Decision:** Escalation writes a `pending` record and a queue index entry into Dapr state, guarded
by ETag compare-and-swap. The verdict endpoint resolves it only while it is still `pending`. No
timers, no background workers, no in-memory queues.

**Consequences:** Surviving a restart is automatic, because nothing waits in memory for a crash to
kill. The approver queue and verdict idempotency come out of the same design with no extra work. One
cost - listing the queue needs a hand-maintained index key, because Dapr's key-value store cannot
run queries.

---

## ADR-008 — The payment saga: persisted steps and a compare-and-swap budget

**Context:** This is the money path, so it has to stay correct even under bad conditions - no
overspending, no orphaned reservations, no double payments. It also has to hold across crashes (an
event redelivered after a half-finished step) and concurrency (two approvals racing for the same
budget). Each of these is graded on its own, so each guarantee should map to a small mechanism I can
test by itself, instead of coming out of the system as a whole. I reused the same "progress is a
record, not a process" idea from the pause, so crash recovery picks up from a marker instead of
redoing or losing work.

**Decision:** Save a step-marker per invoice (`started → reserved → paid | compensated | rejected`)
before each effect. Reserve budget with an ETag compare-and-swap loop. Make the provider idempotent
by keying on the invoice id, and re-check the ceiling on its own as a second line of defense.

**Consequences:** Each money rule maps to one small mechanism I can unit-test on its own, and crash
recovery just resumes from the marker. Two downsides - the compare-and-swap loops are more code than
a plain lock, and a busy budget key would need partitioning at real scale.

---

## ADR-009 — Nginx as the single entry point

**Context:** I need one outside entry point that routes to the services and rate-limits, and the
single-page app needs somewhere to be served from. The choice was a proven reverse proxy (Nginx or
Traefik) or a custom FastAPI gateway that could hold auth logic inside it. Under the deadline, a
gateway written in code is more code to write, test, and defend for a job that plain config already
does. So I went with infrastructure that needs no code and that a grader knows right away.

**Decision:** One Nginx container routes `/api/*` to the services, applies `limit_req` for rate
limiting, and serves the built React static files.

**Consequences:** There is no gateway code to maintain, and it is all familiar and proven. The one
awkward part is JWT auth, which needs a different place. I enforce it inside the services with a
shared `require_role` dependency instead of at the proxy.

---

## ADR-010 — Stateless services, with all state in Dapr (the "millions of users" answer)

**Context:** The assignment says to assume millions of users, so the architecture has to make scale
a config and deployment matter, never a rewrite. Statelessness is what makes that possible. If no
service holds real state in its own process, replicas are interchangeable, each pub/sub stage scales
on its own (decision-svc workers scale with LLM latency, the real bottleneck), and keying by entity
splits the load on its own. This is graded as a design and code-quality question, not something I am
meant to load-test.

**Decision:** No service keeps real in-process state. The one documented exception is notification's
transient store. Every read and write goes through the Dapr state store.

**Consequences:** Horizontal scaling becomes easy, each stage scales on its own, and every stand-in
component is a swap instead of a rewrite. The costs are small - a bit more code than in-memory
shortcuts, and no real load testing.

---

## ADR-011 — Splitting persistence: audit to Postgres, budgets on hardened Redis

**Context:** Two kinds of data with different needs share this system. The audit trail is
append-only and wants strong durability and retention. The money and budget state depends on
compare-and-swap behavior I had already pinned down against the real Redis sidecar, and the
no-overspend proof rests on that. Moving budgets to Postgres this close to the deadline would mean
working out how conflicts show up there all over again - risk with nothing to gain. But leaving
audit on plain Redis would undersell its durability. Since Dapr hides the store behind a component,
the same code can point each one at the store that fits.

**Decision:** The audit trail goes on a Postgres-backed Dapr state component. Budgets stay on Redis,
now hardened with AOF persistence. Both use the same shared state class, pointed at different
components.

**Consequences:** The money path keeps the compare-and-swap behavior I had already tested against
the real sidecar, and audit gets strong durability plus a real polyglot-persistence story. The costs
- one more container, and budget durability now rests on AOF, which in the worst case loses about a
second of acknowledged writes on a hard crash. I accept that and document it.
