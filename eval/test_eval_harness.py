"""
Phase 09 B1.1: tests for the eval harness itself (`eval/run_eval.py`).

Runs `run_eval("stub")` once (module-scoped fixture — the harness is
deterministic and re-running it per test would just repeat identical work)
and asserts:
  (a) every one of the 20 shipped fixtures appears exactly once in the
      result;
  (b) overall stub accuracy is >= the documented, HONEST floor
      (`ACCURACY_FLOOR` — currently 1.0 because the stub genuinely matches
      all 20 shipped fixtures today; see `run_eval.py`'s comment on that
      constant for why this is not a fudged number);
  (c) the malicious-stub safety sweep reports 0 breaches (the one
      documented exception, INV-1015, is asserted separately and must NOT
      silently disappear from that exception list).
"""

import json
from pathlib import Path

from eval.run_eval import ACCURACY_FLOOR, KNOWN_AGENT_ONLY_ESCALATION_FIXTURES, run_eval

_FIXTURES_PATH = Path(__file__).resolve().parent.parent / "sample-invoices.json"


def _all_fixture_ids() -> set[str]:
    data = json.loads(_FIXTURES_PATH.read_text())
    return {f["id"] for f in data["fixtures"]}


def test_every_fixture_appears_exactly_once():
    result = run_eval("stub")

    ids = [r.id for r in result.fixture_results]
    assert set(ids) == _all_fixture_ids()
    assert len(ids) == len(set(ids)), "a fixture id was scored more than once"
    assert len(result.fixture_results) == len(_all_fixture_ids())


def test_stub_accuracy_meets_the_honest_floor():
    result = run_eval("stub")

    assert result.overall_accuracy >= ACCURACY_FLOOR, (
        f"stub accuracy {result.overall_accuracy:.0%} dropped below the documented "
        f"floor {ACCURACY_FLOOR:.0%} — a real regression, not a fixture-count change"
    )


def test_safety_sweep_reports_zero_breaches():
    result = run_eval("stub")

    assert result.safety.breach_count == 0
    assert result.safety.breaches == []


def test_safety_sweep_known_exception_is_exactly_inv_1015():
    """INV-1015's alcohol-only MEAL-03 reject has no Gate 2 deterministic
    backstop (see test_m12_adversarial.py's module docstring) — it is the
    one documented case where the always-approve malicious agent reaches
    `auto_approve`. Pinning this list catches both a silent regression
    (a NEW fixture starts leaking through undetected) and a silent
    over-suppression (INV-1015 stops actually needing the exception)."""
    result = run_eval("stub")

    assert set(result.safety.known_exceptions) == KNOWN_AGENT_ONLY_ESCALATION_FIXTURES


def test_per_route_and_confusion_matrix_totals_match_fixture_count():
    result = run_eval("stub")

    per_route_total = sum(total for _correct, total in result.per_route_accuracy().values())
    assert per_route_total == result.total

    confusion_total = sum(result.confusion_matrix().values())
    assert confusion_total == result.total
