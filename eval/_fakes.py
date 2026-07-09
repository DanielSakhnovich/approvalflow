"""
Minimal in-memory doubles for the eval harness — small, standalone
equivalents of the fixtures/builders in `services/decision/tests/test_pipeline.py`
and `services/decision/tests/malicious.py` (NOT imported directly: those are
pytest test modules, fragile to import into runnable harness code — see
`eval/run_eval.py`'s module docstring).

Each fake implements just the slice of the real repo's interface that
`DecisionPipeline` actually calls (duck-typed — the pipeline takes no
concrete base classes), backed by plain dicts instead of `afcommon.state`'s
etag/CAS machinery, since the eval harness runs single-threaded and needs
no concurrency control.

`fingerprint_of` and `Thresholds`/`AgentRecommendation` ARE imported from
the real production modules (`services/decision/src/fingerprint.py`,
`services/decision/src/config.py`, `services/decision/src/agent.py`) rather
than reimplemented, so the harness's duplicate-detection and threshold
shapes can never silently drift from what production actually uses.
"""

from services.decision.src.agent import AgentRecommendation
from services.decision.src.config import Thresholds
from services.decision.src.fingerprint import fingerprint_of


class CapturingPublisher:
    """Records every published (topic, payload) pair — no I/O."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, topic: str, payload: dict) -> None:
        self.calls.append((topic, payload))


class FakeConfigRepo:
    """Returns the default `Thresholds()` (no overrides) and the fx rates
    passed in at construction — mirrors `ConfigRepo.get_thresholds`'s
    seed-on-first-read default and `get_fx_rates`'s `sample-invoices.json`
    seed, without the real repo's state-store persistence."""

    def __init__(self, fx_rates: dict[str, float]) -> None:
        self._fx_rates = fx_rates

    async def get_thresholds(self) -> Thresholds:
        return Thresholds()

    async def get_fx_rates(self) -> dict[str, float]:
        return self._fx_rates


class FakeFingerprintRegistry:
    """In-memory duplicate registry with the same first-write-wins +
    resubmission-owner semantics as `FingerprintRegistry.check_and_register`
    (Gate 1), backed by a plain dict instead of `try_register`'s CAS.

    Reuses the real `fingerprint_of` so the eval harness computes duplicates
    exactly the way production does."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    async def check_and_register(self, invoice: dict, invoice_id: str) -> bool:
        fp = fingerprint_of(invoice)
        owner = self._seen.get(fp)
        if owner is None:
            self._seen[fp] = invoice_id
            return True
        return owner == invoice_id


class FakeTrustRepo:
    """In-memory vendor+category trust, same shape as `TrustRepo` (earned
    only via `record_paid`/`record_paid_for_invoice`, never on submission
    alone) — backed by plain dicts/sets instead of state-store keys."""

    def __init__(self) -> None:
        self._trusted: set[tuple[str, str]] = set()
        self._invoice_cache: dict[str, tuple[str, str]] = {}

    async def is_trusted(self, vendor: str, category: str) -> bool:
        return (vendor.lower(), category.lower()) in self._trusted

    async def remember_invoice(self, invoice_id: str, vendor: str, category: str) -> None:
        self._invoice_cache[invoice_id] = (vendor, category)

    async def record_paid(self, vendor: str, category: str) -> None:
        self._trusted.add((vendor.lower(), category.lower()))

    async def record_paid_for_invoice(self, invoice_id: str) -> None:
        cached = self._invoice_cache.get(invoice_id)
        if cached is None:
            return
        self._trusted.add((cached[0].lower(), cached[1].lower()))


class AlwaysApproveAgent:
    """Equivalent of `services/decision/tests/malicious.py`'s
    `MaliciousStubAgent`: always recommends approve/1.0 confidence, no
    policy violations, no fraud signals, regardless of invoice content.
    Used by the safety sweep to prove the deterministic router — not the
    agent — is what actually gates autonomy (M12's adversarial guarantee)."""

    async def evaluate(self, invoice: dict, policy_rules: str) -> AgentRecommendation:
        return AgentRecommendation(
            recommendation="approve",
            confidence=1.0,
            policy_violations=[],
            fraud_signals=[],
            reasoning="Always approve (eval-harness malicious stub for the safety sweep).",
        )
