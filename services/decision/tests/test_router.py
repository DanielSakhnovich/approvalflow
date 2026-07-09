"""
Gate 3 — the deterministic router (D-012 posture).

Table-driven coverage of all 8 decision-order branches (first-match-wins),
the boundary conditions (ceiling, confidence), the trusted-vendor uplift,
the trusted-but-capped interaction, and the fact that `duplicate` short-
circuits before the agent is ever consulted -- even a malicious one.
"""

from services.decision.src.agent import AgentRecommendation
from services.decision.src.config import Thresholds
from services.decision.src.router import RouterDecision, route_invoice
from services.decision.src.validators import ValidationResult
from services.decision.tests.malicious import MaliciousStubAgent

T = Thresholds()


def _validation(usd_cents=1000, hard_stops=None, notes=None) -> ValidationResult:
    return ValidationResult(
        usd_cents=usd_cents,
        hard_stops=hard_stops or [],
        notes=notes or [],
    )


def _agent(
    recommendation="approve",
    confidence=0.92,
    policy_violations=None,
    fraud_signals=None,
    reasoning="In policy for its category.",
) -> AgentRecommendation:
    return AgentRecommendation(
        recommendation=recommendation,
        confidence=confidence,
        policy_violations=policy_violations or [],
        fraud_signals=fraud_signals or [],
        reasoning=reasoning,
    )


def _invoice(category="other", attendees=None) -> dict:
    inv = {"category": category}
    if attendees is not None:
        inv["attendees"] = attendees
    return inv


# --- 1. duplicate --------------------------------------------------------


def test_1_duplicate_wins_first():
    d = route_invoice(
        duplicate=True,
        validation=_validation(),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert isinstance(d, RouterDecision)
    assert d.route == "duplicate"
    assert d.violations == ["GLOBAL-DUP"]
    assert "never paid twice" in d.reasoning


async def test_1_duplicate_ignores_a_malicious_agent_entirely():
    """Even MaliciousStubAgent (approve/1.0/no violations) cannot escape
    `duplicate` -- gate 1 fires before the agent's recommendation is
    consulted at all."""
    malicious = await MaliciousStubAgent().evaluate({}, "")
    d = route_invoice(
        duplicate=True,
        validation=_validation(usd_cents=999_999_999, hard_stops=[]),
        agent=malicious,
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "duplicate"


def test_1_duplicate_wins_even_when_amount_exceeds_ceiling():
    """Gate 1 short-circuits before branch 5's ceiling check ever runs: an
    amount well over the effective ceiling must still route `duplicate` with
    ONLY `GLOBAL-DUP` (no `AUTONOMY-CEILING` mixed in), while
    `effective_ceiling_cents` is still the correctly-computed value (the
    ceiling computation runs unconditionally before the duplicate check,
    even though this route never gates on it)."""
    d = route_invoice(
        duplicate=True,
        validation=_validation(usd_cents=999_999),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "duplicate"
    assert d.violations == ["GLOBAL-DUP"]
    assert d.effective_ceiling_cents == 25000


def test_1_duplicate_with_non_numeric_attendees_does_not_raise():
    """A meals invoice with a malformed `attendees` (e.g. the string "2")
    must not crash the ceiling computation even on the `duplicate` branch,
    which runs before any hard-stop/agent checks but still calls
    `_effective_ceiling_cents` first."""
    d = route_invoice(
        duplicate=True,
        validation=_validation(hard_stops=["MEAL-01"]),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees="2"),
    )
    assert d.route == "duplicate"


# --- 2. agent unavailable --------------------------------------------------


def test_2_agent_none_routes_human_review():
    d = route_invoice(
        duplicate=False,
        validation=_validation(hard_stops=["GLOBAL-VENDOR"]),
        agent=None,
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["AGENT-UNAVAILABLE", "GLOBAL-VENDOR"]


def test_2_agent_none_reasoning_is_template_only_no_notes_appended():
    """Route 2 (agent unavailable) is a bare synthesized template -- it must
    NEVER append Gate 2's notes, unlike routes 3-8 which call `_with_notes`."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(
            hard_stops=["GLOBAL-VENDOR"], notes=["Vendor is unknown"]
        ),
        agent=None,
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.reasoning == "Agent unavailable after retries — routing to human review."
    assert "Vendor is unknown" not in d.reasoning


# --- 3. MEAL-03 reject (only reject-severity rule) -------------------------


def test_3_meal03_rejects():
    d = route_invoice(
        duplicate=False,
        validation=_validation(),
        agent=_agent(
            recommendation="reject",
            policy_violations=["MEAL-03"],
            reasoning="Alcohol-only receipts are not reimbursable.",
        ),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees=2),
    )
    assert d.route == "reject"
    assert d.violations == ["MEAL-03"]
    assert d.reasoning == "Alcohol-only receipts are not reimbursable."


def test_3_meal03_wins_over_ceiling_and_confidence():
    """MEAL-03 fires before the ceiling/confidence checks even when both
    would otherwise also escalate -- first-match-wins."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=999_999),
        agent=_agent(confidence=0.10, policy_violations=["MEAL-03"]),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees=2),
    )
    assert d.route == "reject"


# --- 4. hard stops / fraud signals -----------------------------------------


def test_4_hard_stops_route_human_review():
    d = route_invoice(
        duplicate=False,
        validation=_validation(hard_stops=["GLOBAL-RECEIPT"], notes=["Receipt required."]),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["GLOBAL-RECEIPT"]
    assert "Receipt required." in d.reasoning


def test_4_fraud_signals_route_human_review_even_with_clean_validation():
    d = route_invoice(
        duplicate=False,
        validation=_validation(hard_stops=[]),
        agent=_agent(fraud_signals=["GLOBAL-FRAUD"]),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["GLOBAL-FRAUD"]


def test_4_violations_combine_hard_stops_fraud_and_policy():
    d = route_invoice(
        duplicate=False,
        validation=_validation(hard_stops=["GLOBAL-VENDOR"]),
        agent=_agent(fraud_signals=["GLOBAL-FRAUD"], policy_violations=["SAAS-01"]),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["GLOBAL-VENDOR", "GLOBAL-FRAUD", "SAAS-01"]


# --- 5. ceiling + category caps ---------------------------------------------


def test_5_over_ceiling_routes_human_review():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=25001),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CEILING"]
    assert d.effective_ceiling_cents == 25000


def test_5_boundary_usd_cents_equal_to_effective_passes_gate():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=25000),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "auto_approve"


def test_5_boundary_one_cent_over_effective_escalates():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=25001),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert "AUTONOMY-CEILING" in d.violations


def test_5_saas_cap_applies_even_under_base_ceiling():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=20001),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="saas"),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CEILING"]
    assert d.effective_ceiling_cents == 20000


def test_5_meals_cap_is_attendees_times_per_attendee():
    # 3 attendees * $75 = $225 cap -> $225.01 escalates, $225.00 passes.
    ok = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=22500),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees=3),
    )
    assert ok.route == "auto_approve"
    assert ok.effective_ceiling_cents == 22500

    over = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=22501),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees=3),
    )
    assert over.route == "human_review"


def test_5_meals_missing_attendees_does_not_crash_and_uses_no_cap():
    """Gate 2's MEAL-01 hard stop already forces human review when attendees
    is missing for a meals invoice, but the router itself must not crash if
    it somehow still sees a meals invoice without `attendees` -- documented
    router-side defense in depth. With no cap, only the base ceiling
    applies."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=T.ceiling_cents, hard_stops=["MEAL-01"]),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees=None),
    )
    # MEAL-01 hard stop still forces human_review via branch 4, but the
    # ceiling computation itself must not raise (e.g. TypeError on None * int).
    assert d.route == "human_review"
    assert d.effective_ceiling_cents == T.ceiling_cents


def test_5_meals_non_numeric_attendees_does_not_crash_and_uses_no_cap():
    """A meals invoice with a non-numeric `attendees` (e.g. the string "2",
    which Gate 2's MEAL-01 check now rejects) must not crash the router's
    `attendees * meals_per_attendee_cents` multiplication either -- same
    defense-in-depth guard as the missing-attendees case above."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=T.ceiling_cents, hard_stops=["MEAL-01"]),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="meals", attendees="2"),
    )
    assert d.route == "human_review"
    assert d.effective_ceiling_cents == T.ceiling_cents


def test_5_non_capped_category_uses_base_ceiling_only():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=25000),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="hardware"),
    )
    assert d.route == "auto_approve"
    assert d.effective_ceiling_cents == 25000


def test_5_trusted_uplift_travel_no_cap():
    """Travel has no category cap, so a trusted vendor genuinely benefits
    from the $400 uplift ceiling (D-012 correction)."""
    untrusted = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=32000),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="travel"),
    )
    assert untrusted.route == "human_review"
    assert untrusted.effective_ceiling_cents == 25000

    trusted = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=32000),
        agent=_agent(),
        thresholds=T,
        trusted=True,
        invoice=_invoice(category="travel"),
    )
    assert trusted.route == "auto_approve"
    assert trusted.effective_ceiling_cents == 40000


def test_5_trusted_uplift_boundary_equal_to_uplifted_ceiling_passes():
    """Boundary equality at the uplifted ceiling itself ($400.00, no cap for
    travel): exactly the uplifted ceiling still auto-approves."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=40000),
        agent=_agent(),
        thresholds=T,
        trusted=True,
        invoice=_invoice(category="travel"),
    )
    assert d.route == "auto_approve"
    assert d.effective_ceiling_cents == 40000


def test_5_trusted_uplift_boundary_one_cent_over_escalates():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=40001),
        agent=_agent(),
        thresholds=T,
        trusted=True,
        invoice=_invoice(category="travel"),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CEILING"]
    assert d.effective_ceiling_cents == 40000


def test_5_saas_cap_boundary_equal_to_cap_passes():
    """Boundary equality at the SaaS monthly cap ($200.00) itself: exactly
    the cap still auto-approves (only one cent over escalates, per
    `test_5_saas_cap_applies_even_under_base_ceiling`)."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=20000),
        agent=_agent(),
        thresholds=T,
        trusted=False,
        invoice=_invoice(category="saas"),
    )
    assert d.route == "auto_approve"
    assert d.effective_ceiling_cents == 20000


def test_5_trusted_but_capped_saas_stays_human():
    """SaaS is capped at $200/mo regardless of trust -- the trusted-vendor
    uplift raises the *base* ceiling but never the category cap."""
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=30000),
        agent=_agent(),
        thresholds=T,
        trusted=True,
        invoice=_invoice(category="saas"),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CEILING"]
    assert d.effective_ceiling_cents == 20000


# --- 6. confidence -----------------------------------------------------------


def test_6_low_confidence_routes_human_review():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=1000),
        agent=_agent(confidence=0.5),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CONFIDENCE"]


def test_6_boundary_confidence_exactly_min_passes():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=1000),
        agent=_agent(confidence=0.80),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "auto_approve"


def test_6_boundary_confidence_just_under_min_escalates():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=1000),
        agent=_agent(confidence=0.7999),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["AUTONOMY-CONFIDENCE"]


# --- 7. agent policy violations pass through --------------------------------


def test_7_policy_violations_pass_through_as_human_review():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=1000),
        agent=_agent(policy_violations=["TRAVEL-03"]),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "human_review"
    assert d.violations == ["TRAVEL-03"]


# --- 8. auto approve ----------------------------------------------------------


def test_8_clean_invoice_auto_approves_with_agent_reasoning():
    d = route_invoice(
        duplicate=False,
        validation=_validation(usd_cents=1000),
        agent=_agent(reasoning="Clean, in-policy, known vendor."),
        thresholds=T,
        trusted=False,
        invoice=_invoice(),
    )
    assert d.route == "auto_approve"
    assert d.violations == []
    assert d.reasoning == "Clean, in-policy, known vendor."
