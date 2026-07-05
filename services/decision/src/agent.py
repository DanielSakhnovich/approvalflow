"""
The `DecisionAgent` port (D-003): a single interface hiding the volatile part
of the system (the LLM/agent brain) behind a domain-language signature.

`evaluate()` takes a raw invoice dict (fixture shape) and the current policy
text, and returns a structured `AgentRecommendation`. No provider, message, or
token types appear in the signature -- adapters (stub, handrolled, maf) own
that complexity internally.
"""

from typing import Literal, Protocol

from pydantic import BaseModel


class AgentRecommendation(BaseModel):
    recommendation: Literal["approve", "reject", "needs_review"]
    confidence: float
    policy_violations: list[str]
    fraud_signals: list[str]
    reasoning: str


class DecisionAgent(Protocol):
    async def evaluate(self, invoice: dict, policy_rules: str) -> AgentRecommendation: ...
