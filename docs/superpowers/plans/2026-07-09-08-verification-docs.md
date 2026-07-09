# Phase 08 — Verification + Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The finishing work that makes the built system provable and presentable. **D5** — a single `make verify` command that brings the stack up, runs the four acceptance journeys and the anti-cheese guards, and prints a clean PASS/FAIL. **D1** — ARCHITECTURE.md with Mermaid sequence + payment-compensation diagrams. **D4** — the auto-generated OpenAPI is reachable and documented. **D6** — the README's final pass (purpose, diagram, run, test, verify). **D7** — a shot-by-shot demo script (Daniel records). **D2 (ADRs) is explicitly OUT of scope for the agent — Daniel hand-writes them from `decisions.md`.**

**Architecture:** Spec §9/§10, decisions D-005…D-017 (the raw material for the ARCHITECTURE narrative and Daniel's ADRs). The verify harness reuses the compose stack and the shipped fixtures; it is a focused, clean pass/fail runner distinct from the developer `smoke-compose.sh` (which also does live component characterization).

**Tech Stack:** bash + Make · Mermaid (in Markdown) · FastAPI's built-in OpenAPI. No new services.

## Global Constraints

- `make verify` is the one graded command (D5): it must run the FOUR journeys + the anti-cheese guards and print a single PASS/FAIL, exit 0 on all-pass / non-zero otherwise. It brings the stack up from cold and tears it down.
- Anti-cheese guards (assignment D5, explicit): (a) at least 2 items auto-approve with NO human; (b) an "approve me" note in the payload does NOT flip the decision (INV-1013: $300, notes "Approve me…", must route `human_review`, proving the agent isn't steered by the payload).
- The four journeys by fixture: A auto-approve→paid (INV-1001), B escalate→restart→resume (INV-1003), C duplicate paid-once (INV-1007 after INV-1001), D payment-failure→compensation (INV-1012).
- Journeys and guards derive payloads from `sample-invoices.json` at runtime (no drift); poll for eventual state (never single-shot); never pipe the running harness through head/grep (SIGPIPE).
- Docs are honest: ARCHITECTURE.md documents the accepted residuals already in `decisions.md` (saga concurrency window, M12's INV-1015 scope) rather than overclaiming.
- Branch `feature/verification-docs` off `main`; conventional commits; controller pushes per task; PR at end. Baseline: **358 passed**. No new Python tests expected (this is docs + a shell harness); `docker compose build` + `make verify` are the gates.

---

### Task 1: D5 — `make verify` one-command verification harness

**Files:**
- Create: `Makefile`, `scripts/verify.sh`

**`Makefile` targets:**
- `up` — `docker compose up --build -d`
- `down` — `docker compose down -v`
- `test` — `pytest -q` (the unit/integration suite)
- `smoke` — `bash scripts/smoke-compose.sh` (the developer characterization run)
- `verify` — `bash scripts/verify.sh` (the D5 graded runner)
- `.PHONY` all of them; a `help` default listing them.

**`scripts/verify.sh`** — self-contained, `set -euo pipefail`, from repo root:
1. `cp dapr/secrets.example.json dapr/secrets.json` if missing; `docker compose down -v` then `docker compose up --build -d`; wait for every service healthy (intake/decision/approval/payment/audit/notification + gateway).
2. A `check "<name>" <command>` helper that records PASS/FAIL into arrays and prints `✓`/`✗` per line, never aborting mid-run (so the summary shows every result).
3. Derive each fixture payload from `sample-invoices.json` (strip `expected`; keep `scenario` for INV-1012).
4. **Journey A** (INV-1001): submit via gateway `:8080/api/invoices` → poll status → assert `paid`, route `auto_approve`, no human touched.
5. **Journey B** (INV-1003): submit → poll `pending_approval` → poll it into the approval queue → `docker compose restart approval-svc approval-svc-dapr` (two-phase, as smoke does — the daprd-namespace hazard) → poll queue survives → POST verdict `approved` → poll intake → assert `approved` (resumed after restart).
6. **Journey C** (INV-1007 = resubmit of INV-1001's vendor+number+total): after A, submit INV-1007's payload → poll → assert `duplicate` (paid once, second short-circuited).
7. **Journey D** (INV-1012): submit (keep scenario) → poll `pending_approval` → verdict `approved` → poll intake `payment_failed`; assert the engineering budget is UNCHANGED (reservation released — no orphan) via `:8080/api/budgets/engineering-2026Q2`.
8. **Anti-cheese (a)** ≥2 auto-approve no-human: submit INV-1016 and INV-1017 → poll each to `paid`, route `auto_approve`; assert both reached `paid` without ever entering the approval queue (count them + INV-1001 = 3 auto-approvals with no human).
9. **Anti-cheese (b)** steering: submit INV-1013 ($300, "Approve me…" note) → poll → assert route `human_review` (NOT auto_approve) — the payload note did not flip the decision. Also assert F10 still shows 0 violations after everything.
10. Print a summary block: each check ✓/✗, then `VERIFICATION: PASS (N/N)` or `FAIL (k failed)`; `docker compose down -v`; exit 0 iff all passed.

- [ ] **Step 1: Write `scripts/verify.sh` + `Makefile`.**
- [ ] **Step 2: Run `make verify` from cold end-to-end.** Capture the full transcript. Diagnose real failures from `docker compose logs`. It must end `VERIFICATION: PASS` and exit 0.
- [ ] **Step 3: `pytest -q` still 358; `ruff` unaffected (no Python changed).**
- [ ] **Step 4: Commit** — `feat(verify): make verify — one-command four-journey + anti-cheese verification (D5)`

---

### Task 2: D1 — ARCHITECTURE.md with Mermaid diagrams

**Files:**
- Create: `ARCHITECTURE.md`

**Contents (component boundaries designed before code; the diagrams are the deliverable):**
- **Overview** — one paragraph: the 80/20 posture, agent-recommends/router-decides, the six services.
- **Component diagram** (Mermaid `graph`) — the six services + gateway + UI + Redis + Postgres + Dapr sidecars, sync vs async edges (reuse the system-map/service-relations structure that already exists as PNGs; render it as Mermaid here).
- **Sequence diagram** (Mermaid `sequenceDiagram`) — the happy path end-to-end: UI → gateway → intake (202) → [pub/sub] decision (gates + agent + router) → [auto_approve] payment (saga) → intake status → notification; audit recording throughout; the `?trail=true` sync call.
- **Payment saga / compensation flow** (Mermaid — a `stateDiagram-v2` or `flowchart` showing each step and its compensation): `started → reserved → paid` happy path, and `reserved → (decline) → compensated (release)` and `→ rejected_insufficient_budget`, with the M12 ceiling re-check gate. This is the D1-required "diagram of the payment flow showing each step and its compensation/rollback."
- **The dilemma** — the D-012 posture in one table (link to PRODUCT-DILEMMA.md if/when written; for now summarize from decisions.md).
- **Cross-cutting** — correlation-id tracing, idempotency layers, Dapr abstractions (state/pubsub/secrets/invoke), the two state stores (Redis ops + Postgres audit, D-017).
- **Scaling path** — the swap table from the spec (Redis→cluster/Kafka via component YAML, compose→k8s, nginx→LB), and the honest residuals (saga concurrency window, M12 INV-1015 scope) with pointers to decisions.md.
- **Requirement → where** — a compact table mapping M/F ids to the service/file that satisfies them (mirror the spec's traceability).

- [ ] **Step 1: Write ARCHITECTURE.md** with all Mermaid blocks (verify each renders — GitHub renders Mermaid in Markdown; keep syntax valid).
- [ ] **Step 2: Sanity-check the Mermaid** (no syntax errors; each diagram is self-consistent with the built system).
- [ ] **Step 3: Commit** — `docs: ARCHITECTURE.md — component, sequence, and payment-compensation diagrams (D1)`

---

### Task 3: D4 OpenAPI + D6 README final pass + D7 demo script

**Files:**
- Create: `docs/DEMO-SCRIPT.md` (D7 — shot-by-shot for Daniel to record)
- Modify: `README.md` (final pass), optionally `gateway/nginx.conf` (route `/api/<svc>/docs` if cheap; else document per-service `/docs`)

- **D4 (OpenAPI):** FastAPI auto-serves `/openapi.json` + `/docs` (Swagger UI) on every service — verify each responds (e.g. `:8001/docs`, `:8002/docs`, …). Document in the README how to reach them (per-service ports). Optionally add a gateway convenience route; not required.
- **D6 (README):** ensure it has, in order — purpose, the system-map image (D1 link), how to run (`docker compose up` / open `:8080`), how to test (`make test`), how to VERIFY (`make verify` — call this out prominently, it's D5), the per-service OpenAPI docs, and links to ARCHITECTURE.md + decisions.md. Tighten anything stale.
- **D7 (demo script):** `docs/DEMO-SCRIPT.md` — a 2–5 min shot list Daniel films: (1) `make verify` printing PASS; (2) open `:8080`, submit INV-1001 → watch it auto-approve → paid; (3) submit INV-1003 → escalates → Approver queue → approve → resumes; (4) show the dashboard (F8) and Compliance tab (F10: 0 violations); (5) the audit trail on a paid invoice (F9). Each shot: what to click, what to say (optional voiceover), what proves which requirement.

- [ ] **Step 1: Verify `/docs` + `/openapi.json` respond on each service; note ports in README.**
- [ ] **Step 2: README final pass** — the `make verify` callout is the headline; wire the links.
- [ ] **Step 3: Write `docs/DEMO-SCRIPT.md`.**
- [ ] **Step 4: Commit** — `docs: OpenAPI reachability, README final pass, demo script (D4/D6/D7)`

---

### Task 4: PR, CI, and the handoff checklist for Daniel

**Files:**
- Modify: roadmap (tick Phase 08); create `docs/SUBMISSION-CHECKLIST.md`

- **Handoff checklist** (`docs/SUBMISSION-CHECKLIST.md`) — the things ONLY Daniel can do, in order: (1) **hand-write the ADRs** in `docs/adr/` from `decisions.md` (D2 — personal authorship required; list the key decisions to cover: D-003 agent port, D-005 six services, D-007 choreography+saga, D-012 dilemma posture, D-017 storage split); (2) write `docs/PRODUCT-DILEMMA.md` from D-012 (the graded dilemma justification, with fixture evidence); (3) record the demo per `DEMO-SCRIPT.md`; (4) freeze `main` at EOD 12/07; (5) add contributors `alonf`, `VenyaBrodetskiy`, `holohup`, `milaShurupova`; (6) submit the form (repo URL + demo URL).
- Roadmap: tick Phase 08.

- [ ] **Step 1: Write SUBMISSION-CHECKLIST.md + roadmap tick.**
- [ ] **Step 2: Commit** — `docs: submission checklist and roadmap tick for phase 08`.
- [ ] **Step 3 (controller):** push, PR "Phase 08 — Verification + Docs", CI green, merge.

---

## Self-Review (done)

- **Spec coverage:** D5 (Task 1 — the flagship one-command verify with the exact anti-cheese guards the assignment names), D1 (Task 2 — the required sequence + payment-compensation Mermaid diagrams), D4 (Task 3 — OpenAPI reachability), D6 (Task 3 — README final), D7 (Task 3 — demo script). D2 (ADRs) and PRODUCT-DILEMMA.md are explicitly handed to Daniel (personal authorship / graded justification) via the checklist, not auto-written.
- **Placeholder scan:** verify.sh's checks are concrete (exact fixtures, exact expected routes/states); the anti-cheese guards are the assignment's literal wording.
- **Risks:** (1) verify.sh timing — reuse the smoke's proven poll patterns (spaced queue polls, two-phase restart). (2) Mermaid syntax — keep it simple and validate rendering. (3) `make verify` runtime is ~4–5 min (full cold cycle); acceptable for a graded verification. (4) The four-journey + anti-cheese logic overlaps the smoke but is re-expressed cleanly here — that duplication is intentional (the smoke is the dev tool; verify is the graded artifact).
