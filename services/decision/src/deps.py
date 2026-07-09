import os

import httpx
from afcommon.dedupe import EventDedupe
from afcommon.events import publish
from afcommon.state import DaprStateStore

from .agent import DecisionAgent
from .agents.handrolled import HandrolledAgent
from .agents.stub import StubAgent
from .config import ConfigRepo, _find_repo_file
from .fingerprint import FingerprintRegistry
from .pipeline import DecisionPipeline
from .retrieval import PolicyRetriever, chunk_policy
from .trust import TrustRepo

_config_repo: ConfigRepo | None = None
_trust_repo: TrustRepo | None = None
_dedupe: EventDedupe | None = None
_pipeline: DecisionPipeline | None = None

_SECRETS_URL = "http://localhost:3500/v1.0/secrets/localsecrets/openrouter-api-key"
_OPENROUTER_MODEL = "openai/gpt-4o-mini"


def get_config_repo() -> ConfigRepo:
    global _config_repo
    if _config_repo is None:
        _config_repo = ConfigRepo(DaprStateStore())
    return _config_repo


def get_trust() -> TrustRepo:
    global _trust_repo
    if _trust_repo is None:
        _trust_repo = TrustRepo(DaprStateStore())
    return _trust_repo


def get_dedupe() -> EventDedupe:
    global _dedupe
    if _dedupe is None:
        _dedupe = EventDedupe(DaprStateStore())
    return _dedupe


async def _openrouter_api_key() -> str:
    """M5: pull the OpenRouter key from the Dapr secrets API first, falling
    back to the OPENROUTER_API_KEY env var (e.g. local dev without a
    sidecar). Fails loud if neither is available -- a handrolled agent with
    no key would just fail every request anyway, so surface that at startup.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_SECRETS_URL)
            resp.raise_for_status()
            secret = resp.json().get("openrouter-api-key")
            if secret:
                return secret
    except httpx.HTTPError:
        pass
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    raise RuntimeError(
        "no OpenRouter API key available: Dapr secrets store unreachable and "
        "OPENROUTER_API_KEY is unset"
    )


async def _build_agent() -> DecisionAgent:
    provider = os.environ.get("AGENT_PROVIDER", "stub")
    if provider == "handrolled":
        api_key = await _openrouter_api_key()
        return HandrolledAgent(api_key=api_key, model=_OPENROUTER_MODEL)
    return StubAgent()


async def get_pipeline() -> DecisionPipeline:
    global _pipeline
    if _pipeline is None:
        config = get_config_repo()
        trust = get_trust()
        fingerprints = FingerprintRegistry(DaprStateStore())
        agent = await _build_agent()
        policy_rules = _find_repo_file("policy.md").read_text()
        # N5: `PolicyRetriever` built once from the same policy.md text --
        # `policy_rules` (the full text) stays the fallback the pipeline
        # falls back to when RAG_ENABLED is off or retrieval is unavailable.
        retriever = PolicyRetriever(chunk_policy(policy_rules))
        _pipeline = DecisionPipeline(
            config=config,
            fingerprints=fingerprints,
            trust=trust,
            agent=agent,
            publisher=publish,
            policy_rules=policy_rules,
            retriever=retriever,
        )
    return _pipeline
