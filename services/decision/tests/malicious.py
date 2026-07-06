"""
Test-only malicious adapter (D-003): always recommends approve/1.0 with no
violations or fraud signals, regardless of invoice content. Used to prove
that the deterministic router (Task 6+), not the agent, is what actually
gates autonomy -- a compromised/malicious agent alone must never be able to
auto-approve anything.
"""

from services.decision.src.agent import AgentRecommendation


class MaliciousStubAgent:
    async def evaluate(self, invoice: dict, policy_rules: str) -> AgentRecommendation:
        return AgentRecommendation(
            recommendation="approve",
            confidence=1.0,
            policy_violations=[],
            fraud_signals=[],
            reasoning="Always approve (malicious stub for router-trust tests).",
        )
