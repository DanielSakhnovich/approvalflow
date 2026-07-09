# ApprovalFlow — Submission Checklist (Daniel)

The build is complete: all must-haves M1–M18 are implemented and merged, `make verify` passes 7/7,
and the docs are in place. What remains is the work **only you can do** — the hand-authored,
personally-owned deliverables and the submission mechanics. In order:

> **Deadline:** `main` frozen EOD **2026-07-12**. The items below are yours; everything an agent
> could build is done.

---

## 1. Hand-write the ADRs — `docs/adr/` (D2 — personal authorship required)

The grader expects **your** architecture decision records, not auto-generated ones. `decisions.md`
(D-001…D-017) is the raw material — each decision already has the alternatives, advantages, and
disadvantages. Turn the load-bearing ones into short ADRs in your own words (context → decision →
consequences). Cover at least these:

- [ ] **D-003** — hand-rolled `DecisionAgent` port, MAF-shaped (agent tech: the middle path)
- [ ] **D-005** — six services (bounded contexts) vs a smaller split
- [ ] **D-007** — event choreography + a payment saga (vs orchestration)
- [ ] **D-012** — the autonomy posture (ceiling / trust uplift / category caps / confidence / hard
      stops) — the dilemma resolution
- [ ] **D-017** — the storage split (audit → Postgres, operational state → Redis+AOF)

Suggested format per ADR: `docs/adr/NNNN-short-title.md` with **Status**, **Context**, **Decision**,
**Consequences**. Keep them short; the reasoning already lives in `decisions.md` — the ADR is your
distilled, authored version.

## 2. Write `docs/PRODUCT-DILEMMA.md` (graded dilemma justification)

The graded "product dilemma" write-up: **when should the system act autonomously vs. ask a human?**
Base it on **D-012**. Argue the posture with the fixture evidence:

- [ ] State the posture: ≤ $250 ceiling (≤ $400 for a trusted vendor+category), category caps
      (SaaS ≤ $200/mo, meals ≤ $75/attendee), confidence ≥ 0.80, and the hard stops that always force
      a human (new vendor, FX, math mismatch, fraud signal, missing receipt/info).
- [ ] Justify the trade-off: the cost of a wrong auto-approval vs. the cost of over-escalating and
      making the humans the bottleneck (the F6 "no rubber-stamping" tension).
- [ ] Cite the evidence: the adversarial suite proves the ceiling holds 19/20 fixtures deterministically;
      name the honest residual (INV-1015/MEAL-03 is agent-semantic, bounded by the ceiling — see
      `decisions.md` M12 note) rather than overclaiming.

## 3. Record the demo

- [ ] Follow **[`docs/DEMO-SCRIPT.md`](DEMO-SCRIPT.md)** (shots 0–5 + closing, ~4 min).
- [ ] Upload it; keep the URL for the submission form.

## 4. Freeze `main`

- [ ] Confirm CI is green on `main` and `make verify` passes locally one last time.
- [ ] Freeze `main` at **EOD 2026-07-12** (no further commits after this point).

## 5. Add the graders as contributors

On the GitHub repo (Settings → Collaborators), add:

- [ ] `alonf`
- [ ] `VenyaBrodetskiy`
- [ ] `holohup`
- [ ] `milaShurupova`

## 6. Submit the form

- [ ] Submit the course form with the **repo URL** and the **demo URL**.

---

## Done for you (no action needed — reference)

- Six services + nginx gateway + React SPA, one `docker compose up` (M3/M4/M6/M7).
- Async intake, provable ceiling (M12, router + adversarial suite + payment re-check), durable HITL
  (M11), payment saga + compensation (M9), idempotency (M10), audit trail on Postgres (F9/F10),
  correlation-id tracing + fail-loud agent ACL (M14/M15), CI (ruff + pytest + image build, M16/M17).
- `make verify` (D5, 7/7), `ARCHITECTURE.md` (D1), README final pass (D6), OpenAPI reachable (D4),
  demo script (D7).
- Every decision logged in `decisions.md` (D-001…D-017) — the source for your ADRs above.
