"""
Gate 3 — the deterministic router (D-012 posture, M12).

`route_invoice` is the single point that decides the final `Route`. It is a
pure function: no I/O, no state, no awaits. Everything it needs arrives as
arguments, and its output depends only on those arguments -- this is what
makes the M12 adversarial proof possible: the same function is exercised
directly in tests with a malicious agent, with no service scaffolding able
to hide a side channel back to "approve".

The agent (`AgentRecommendation`) only *recommends*. This router *decides*.
Concretely: nothing the agent returns can widen an outcome past what
deterministic validation (Gate 2 hard stops) or the configured ceiling/caps
already allow -- a compromised or malicious agent that always reports
"approve, confidence 1.0, no violations, no fraud signals" can still only
ever reach `auto_approve` for invoices that were already clean and within
the effective ceiling on every deterministic axis this router checks.

Decision order (exact; first match wins) -- see docs/superpowers/plans/
2026-07-05-03-decision.md Task 6 and .superpowers/sdd/task-6-brief.md:

1. `duplicate` -> `duplicate` (GLOBAL-DUP). Gate 1 always wins; the agent is
   never even consulted for this branch.
2. `agent is None` (provider exhausted retries, M15) -> `human_review`
   (AGENT-UNAVAILABLE + any Gate 2 hard stops) -- never a blind guess.
3. `"MEAL-03" in agent.policy_violations` -> `reject`. The *only*
   reject-severity rule. Alcohol-only detection is semantic (line-item /
   notes text), so it is intentionally agent-side with no Gate 2 backstop --
   see the module docstring in test_m12_adversarial.py for why this is a
   deliberately scoped, orthogonal-to-M12 limitation rather than an oversight.
4. Gate 2 hard stops non-empty OR agent fraud signals non-empty ->
   `human_review`. This is the primary defense-in-depth branch: fraud
   signals are agent-supplied text, but every hard-stop-eligible shipped
   fixture is *also* caught here via Gate 2 alone, independent of the agent.
5. Ceiling + category caps -> `human_review` (AUTONOMY-CEILING) if
   `usd_cents` exceeds the effective ceiling. This is M12's core claim: the
   effective ceiling is computed from `Thresholds` and the invoice's own
   category/attendees, never from anything the agent said, so no agent
   output can raise it.
6. Low confidence -> `human_review` (AUTONOMY-CONFIDENCE).
7. Any remaining agent policy violation -> `human_review` (violations pass
   through as-is).
8. Otherwise -> `auto_approve`, reasoning = the agent's own reasoning.
"""

from afcommon.contracts import Route
from pydantic import BaseModel

from .agent import AgentRecommendation
from .config import Thresholds
from .validators import ValidationResult


class RouterDecision(BaseModel):
    route: Route
    violations: list[str]
    reasoning: str
    effective_ceiling_cents: int


def _with_notes(reasoning: str, notes: list[str]) -> str:
    """Append Gate 2's plain-language stop notes to the agent's reasoning
    (routes 3-8 per the decision-order doc only; routes 1-2 never call this
    helper -- they use synthesized templates instead, verbatim, with no
    notes appended)."""
    if not notes:
        return reasoning
    return reasoning + " " + " ".join(notes)


def _effective_ceiling_cents(thresholds: Thresholds, trusted: bool, invoice: dict) -> int:
    """
    base = the trusted-uplift ceiling if `trusted`, else the standard
    ceiling. category cap: saas -> flat monthly cap; meals -> attendees *
    per-attendee cap. No cap for any other category (including a missing/
    unrecognized one).

    Subtlety: meals normally always carries a valid `attendees` value by the
    time it reaches the router -- Gate 2's MEAL-01 hard stop already forces
    human_review whenever a meals invoice lacks a usable attendee count, so
    branch 4 above catches that case regardless of what this function
    computes. But this function must still not crash if `attendees` is
    somehow missing or malformed anyway (defense in depth): the meals cap is
    applied ONLY when `attendees` passes the full type guard --
    `isinstance(attendees, int) and not isinstance(attendees, bool) and
    attendees >= 1` (same guard as MEAL-01, so `bool` -- an `int` subclass --
    and non-numeric types like `str`/`list` are excluded too) -- otherwise
    there is no meals cap at all (treated the same as an uncapped category),
    rather than raising a `TypeError` from `"2" * int` or mispricing the cap
    from a bool/zero/negative value.
    """
    base = thresholds.trusted_ceiling_cents if trusted else thresholds.ceiling_cents
    category = invoice.get("category")

    cap: int | None = None
    if category == "saas":
        cap = thresholds.saas_monthly_cap_cents
    elif category == "meals":
        attendees = invoice.get("attendees")
        if (
            isinstance(attendees, int)
            and not isinstance(attendees, bool)
            and attendees >= 1
        ):
            cap = attendees * thresholds.meals_per_attendee_cents

    return min(base, cap) if cap is not None else base


def route_invoice(
    *,
    duplicate: bool,
    validation: ValidationResult,
    agent: AgentRecommendation | None,
    thresholds: Thresholds,
    trusted: bool,
    invoice: dict,
) -> RouterDecision:
    effective = _effective_ceiling_cents(thresholds, trusted, invoice)

    # 1. Duplicate always wins -- the agent is never consulted.
    if duplicate:
        return RouterDecision(
            route="duplicate",
            violations=["GLOBAL-DUP"],
            reasoning="Already processed — a submission is never paid twice.",
            effective_ceiling_cents=effective,
        )

    # 2. Agent unavailable (M15 fail-loud): never guess. Route 2 is a bare
    # synthesized template -- no notes appended (see decision-order doc and
    # `_with_notes`'s docstring: routes 1-2 never call it).
    if agent is None:
        return RouterDecision(
            route="human_review",
            violations=["AGENT-UNAVAILABLE", *validation.hard_stops],
            reasoning="Agent unavailable after retries — routing to human review.",
            effective_ceiling_cents=effective,
        )

    # 3. MEAL-03: the only reject-severity rule.
    if "MEAL-03" in agent.policy_violations:
        return RouterDecision(
            route="reject",
            violations=["MEAL-03"],
            reasoning=_with_notes(agent.reasoning, validation.notes),
            effective_ceiling_cents=effective,
        )

    # 4. Deterministic hard stops or agent-reported fraud signals.
    if validation.hard_stops or agent.fraud_signals:
        violations = [*validation.hard_stops, *agent.fraud_signals, *agent.policy_violations]
        return RouterDecision(
            route="human_review",
            violations=violations,
            reasoning=_with_notes(agent.reasoning, validation.notes),
            effective_ceiling_cents=effective,
        )

    # 5. Autonomy ceiling / category cap.
    if validation.usd_cents > effective:
        return RouterDecision(
            route="human_review",
            violations=["AUTONOMY-CEILING"],
            reasoning=_with_notes(agent.reasoning, validation.notes),
            effective_ceiling_cents=effective,
        )

    # 6. Confidence floor.
    if agent.confidence < thresholds.min_confidence:
        return RouterDecision(
            route="human_review",
            violations=["AUTONOMY-CONFIDENCE"],
            reasoning=_with_notes(agent.reasoning, validation.notes),
            effective_ceiling_cents=effective,
        )

    # 7. Any remaining agent policy violation passes through as-is.
    if agent.policy_violations:
        return RouterDecision(
            route="human_review",
            violations=list(agent.policy_violations),
            reasoning=_with_notes(agent.reasoning, validation.notes),
            effective_ceiling_cents=effective,
        )

    # 8. Clean on every axis: auto-approve.
    return RouterDecision(
        route="auto_approve",
        violations=[],
        reasoning=agent.reasoning,
        effective_ceiling_cents=effective,
    )
