"""
The wired decision pipeline (Task 7): the single place that runs an invoice
through every gate, in order, and publishes the result.

Order (exact, matches decisions.md / router.py's decision-order doc):

1. Gate 1 -- fingerprint (`FingerprintRegistry`). Duplicates NEVER reach the
   agent (D-011): if this invoice is a duplicate, the agent is skipped
   entirely -- not called with a throwaway result, not called and ignored,
   simply never invoked. This is both a cost control and a correctness
   property (a malicious/compromised agent gets zero opportunities to act on
   a resubmission).
2. Gate 2 -- deterministic validation (`validate()`). Always runs, even for
   duplicates: `usd_cents` is needed for the published `DecisionMadePayload`
   regardless of route, and it's a pure, cheap, side-effect-free computation.
3. Trust lookup (`TrustRepo.is_trusted`) -- per vendor+category, earned only
   by a prior completed payment (never by anything on this request).
4. Agent (skipped for duplicates, per #1). `ProviderUnavailable` (M15: the
   provider is out of retries) is caught here and mapped to `agent=None` --
   never a silent guess, never an unhandled 500 from a flaky adapter.
5. `route_invoice` -- the deterministic router decides the final route from
   #1-#4's outputs. Nothing here can widen the router's own guarantees.
6. Build + publish a fresh `DecisionMadePayload` (same invoice/correlation
   ids, a fresh event id per D-011/D-016 event identity rules, `usd_cents`
   from Gate 2, `ceiling_cents` = the router's own effective ceiling) and
   return it.

The logging contextvars (correlation_id/invoice_id) are bound once by the
caller -- the Dapr subscription handler, via afcommon's `bind_event_context`,
D-016 -- before `handle_submission` runs, not re-bound per gate here; every
gate still logs its own outcome, so a single invoice's journey through every
gate is traceable from those contextvars alone.
"""

import logging
import os
from collections.abc import Awaitable, Callable

from afcommon.contracts import DecisionMadePayload, InvoiceSubmittedPayload
from afcommon.events import TOPIC_DECISION_MADE, new_event_meta

from .agent import DecisionAgent
from .agents.handrolled import ProviderUnavailable
from .config import ConfigRepo
from .fingerprint import FingerprintRegistry
from .retrieval import PolicyRetriever
from .router import route_invoice
from .trust import TrustRepo
from .validators import validate

log = logging.getLogger(__name__)

Publisher = Callable[[str, dict], Awaitable[None]]


def _rag_enabled() -> bool:
    """N5: `RAG_ENABLED` gates whether the pipeline narrows the agent's
    prompt via `PolicyRetriever`. Read at call time (not cached at process
    start), mirroring afcommon.auth's `AUTH_ENABLED` pattern -- default
    TRUE when unset (opt-out, unlike AUTH_ENABLED's opt-in default)."""
    return os.environ.get("RAG_ENABLED", "true").strip().lower() != "false"


class DecisionPipeline:
    def __init__(
        self,
        config: ConfigRepo,
        fingerprints: FingerprintRegistry,
        trust: TrustRepo,
        agent: DecisionAgent,
        publisher: Publisher,
        policy_rules: str,
        retriever: PolicyRetriever | None = None,
    ):
        self._config = config
        self._fingerprints = fingerprints
        self._trust = trust
        self._agent = agent
        self._publisher = publisher
        self._policy_rules = policy_rules
        self._retriever = retriever

    async def handle_submission(self, payload: InvoiceSubmittedPayload) -> DecisionMadePayload:
        invoice = payload.invoice
        invoice_id = payload.meta.invoice_id
        vendor = str(invoice.get("vendor", ""))
        category = str(invoice.get("category", ""))

        # Gate 1: fingerprint. Duplicate == NOT first sight / not the owner's
        # own resubmission.
        first_sight = await self._fingerprints.check_and_register(invoice, invoice_id)
        duplicate = not first_sight
        log.info("gate 1 (fingerprint): invoice_id=%s duplicate=%s", invoice_id, duplicate)

        thresholds = await self._config.get_thresholds()
        fx_rates = await self._config.get_fx_rates()

        # Gate 2: deterministic validation. Always runs (needed for
        # usd_cents on the published payload regardless of route).
        validation = validate(invoice, fx_rates, thresholds)
        log.info(
            "gate 2 (validate): invoice_id=%s usd_cents=%s hard_stops=%s",
            invoice_id, validation.usd_cents, validation.hard_stops,
        )

        # Trust lookup: earned only by a prior completed payment.
        trusted = await self._trust.is_trusted(vendor, category)
        log.info(
            "trust lookup: invoice_id=%s vendor=%s category=%s trusted=%s",
            invoice_id, vendor, category, trusted,
        )
        # Cache vendor/category now so a later payment-completed event (which
        # carries neither) can still credit the right trust bucket.
        await self._trust.remember_invoice(invoice_id, vendor, category)

        # Agent: skipped entirely for duplicates (D-011) -- never called,
        # not called-and-ignored. Retrieval is likewise skipped for
        # duplicates: there's no agent call for it to narrow the prompt for.
        recommendation = None
        retrieved_rules: list[str] = []
        if duplicate:
            log.info("agent skipped: invoice_id=%s is a duplicate", invoice_id)
        else:
            # N5: RAG narrows the prompt text handed to the agent but must
            # NEVER change the router's decision -- only `policy_rules`
            # (prompt content) and `retrieved_rules` (the audit record of
            # what was retrieved) are affected by this branch.
            policy_rules = self._policy_rules
            if _rag_enabled() and self._retriever is not None:
                retrieved_rules = self._retriever.retrieve(invoice)
                policy_rules = self._retriever.render(retrieved_rules)
                log.info(
                    "retrieved rules: invoice_id=%s ids=%s", invoice_id, retrieved_rules,
                )
            try:
                recommendation = await self._agent.evaluate(invoice, policy_rules)
                log.info(
                    "agent: invoice_id=%s recommendation=%s confidence=%s",
                    invoice_id, recommendation.recommendation, recommendation.confidence,
                )
            except ProviderUnavailable as exc:
                log.warning("agent unavailable: invoice_id=%s error=%s", invoice_id, exc)
                recommendation = None

        decision = route_invoice(
            duplicate=duplicate,
            validation=validation,
            agent=recommendation,
            thresholds=thresholds,
            trusted=trusted,
            invoice=invoice,
        )
        log.info(
            "router: invoice_id=%s route=%s violations=%s",
            invoice_id, decision.route, decision.violations,
        )

        result = DecisionMadePayload(
            meta=new_event_meta(invoice_id, payload.meta.correlation_id),
            route=decision.route,
            recommendation=recommendation.recommendation if recommendation else "n/a",
            confidence=recommendation.confidence if recommendation else None,
            violations=decision.violations,
            reasoning=decision.reasoning,
            usd_cents=validation.usd_cents,
            ceiling_cents=decision.effective_ceiling_cents,
            scenario=invoice.get("scenario", "") or "",
            department=invoice.get("department", "") or "",
            retrieved_rules=retrieved_rules,
        )
        await self._publisher(TOPIC_DECISION_MADE, result.model_dump())
        return result
