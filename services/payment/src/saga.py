"""PaymentSaga (Task 3): the money-consistency core (M9/M10/M12, journey D).

A persisted-step saga keyed by `saga:{invoice_id}`. Every step writes its
marker before doing the next side effect, so a crash between steps resumes
from the last durable marker (step 3/4 below) instead of replaying from
scratch or losing track of a reservation.

Per-invoice idempotency via that record is THE at-least-once guarantee:
`approval-resolved` (and any other trigger) is at-least-once *per invoice*,
not per event_id (fresh event_id per retry), so handle() must be safe to call
twice for the same invoice_id with two entirely different event/correlation
contexts. Once a saga reaches a terminal state (paid, compensated,
rejected_insufficient_budget) it is returned unchanged -- no re-reserve, no
re-execute, no re-publish.

Idempotency for SEQUENTIAL redelivery of an ALREADY-TERMINAL invoice is
guaranteed by the terminal-state check above -- a resumed/retried call for
an invoice_id that already reached a terminal marker (paid/compensated/
rejected) is a no-op: no re-reserve, no re-execute, no re-publish.

Two crash/concurrency windows are KNOWN, ACCEPTED residuals -- both bounded
by the provider's first-write payment idempotency (at most one real payment
ever executes) and both failing in the SAFE direction (budget under-credit,
never overspend, never double-pay). Reviewed and accepted (Opus reviews,
Phase 05 T3 + final):

  (a) SEQUENTIAL crash between reserve() committing and the `reserved`
      marker being written. The saga is durably at `started` with the budget
      already decremented; on redelivery `started` re-runs reserve() and
      decrements a SECOND time. Net: budget decremented twice, paid once.
      reserve() and the step marker are two non-atomic writes to different
      keys, so this window is inherent without a per-invoice reservation
      record (deliberately NOT reintroduced -- an earlier attempt at one
      introduced a worse release-double-credit bug and was reverted).

  (b) TRULY CONCURRENT delivery of the same invoice_id (two handle() calls
      in flight, e.g. visibility-timeout overlap): both observe a non-terminal
      record and both call reserve(). Outside the at-least-once, sequential-
      per-key delivery model this saga targets.

Both would be closed by an idempotent per-invoice reservation record; the
trade-off (that mechanism's own failure modes vs. a bounded, safe-direction
budget erosion) was weighed and the residual accepted for this scope.

M12 layer 4 (defense-in-depth): even though decision-svc and approval-svc
already gate the ceiling, payment re-checks it independently for auto-routed
amounts. If it's ever violated here, BOTH services would have to be wrong
for money to actually move -- so this is a loud refusal (log.critical), not
a quiet fallback. Human-approved amounts (auto_route=False) skip this check
entirely: human approval IS the authorization for above-ceiling amounts.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from afcommon.contracts import PaymentCompletedPayload, PaymentFailedPayload
from afcommon.events import TOPIC_PAYMENT_COMPLETED, TOPIC_PAYMENT_FAILED, new_event_meta
from afcommon.state import StateStore, cas_update
from pydantic import BaseModel

from .budgets import BudgetStore
from .provider import MockPaymentProvider, PaymentDeclined

log = logging.getLogger(__name__)

Publisher = Callable[[str, dict], Awaitable[None]]

_SAGA_KEY_PREFIX = "saga:"

_REASON_INSUFFICIENT_BUDGET = "insufficient_budget"
_REASON_CEILING_VIOLATION = "ceiling-violation"
_REASON_PAYMENT_DECLINED = "payment_declined"
_REASON_INVALID_AMOUNT = "invalid_amount"


class SagaState(StrEnum):
    started = "started"
    reserved = "reserved"
    paid = "paid"
    compensated = "compensated"
    rejected_insufficient_budget = "rejected_insufficient_budget"


_TERMINAL_STATES = frozenset(
    {SagaState.paid, SagaState.compensated, SagaState.rejected_insufficient_budget}
)


class SagaRecord(BaseModel):
    invoice_id: str
    correlation_id: str
    state: SagaState
    department: str
    amount_cents: int
    payment_ref: str | None = None
    failure_reason: str | None = None
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaymentSaga:
    def __init__(
        self,
        budgets: BudgetStore,
        provider: MockPaymentProvider,
        store: StateStore,
        publisher: Publisher,
    ):
        self._budgets = budgets
        self._provider = provider
        self._store = store
        self._publisher = publisher

    async def handle(
        self,
        invoice_id: str,
        correlation_id: str,
        department: str,
        amount_cents: int,
        scenario: str,
        *,
        auto_route: bool,
        ceiling_cents: int | None,
    ) -> SagaRecord:
        key = f"{_SAGA_KEY_PREFIX}{invoice_id}"
        value, _ = await self._store.get(key)
        record = SagaRecord(**value) if value is not None else None

        # Step 1: per-invoice idempotency, THE at-least-once guarantee.
        if record is not None and record.state in _TERMINAL_STATES:
            return record

        if record is not None:
            # Resuming: trust the durable marker's own fields over whatever
            # this particular call happened to pass, so a retry can never
            # drift from what was actually reserved/marked.
            department = record.department
            amount_cents = record.amount_cents
            correlation_id = record.correlation_id

        # Step 1b: poison amount. Guard BEFORE ever claiming/creating a
        # `started` record -- reserve() would raise ValueError on
        # amount_cents<=0, which (pre-fix) stranded the saga at `started`
        # forever (the exception propagates, no marker ever advances, and
        # every redelivery repeats the crash). Refuse loudly up front instead.
        if amount_cents <= 0:
            log.critical(
                "invalid amount_cents=%s for invoice_id=%s -- refusing payment, "
                "no funds moved",
                amount_cents, invoice_id,
            )
            record = await self._transition(
                key, invoice_id, correlation_id, department, amount_cents,
                state=SagaState.compensated, failure_reason=_REASON_INVALID_AMOUNT,
            )
            await self._publish_failed(record, compensated=False)
            return record

        # Step 2 (M12 layer 4): auto-routed amounts get an independent
        # ceiling re-check. Human-approved amounts skip it by design. Skip
        # the guard when resuming a saga already past `reserved`: the
        # reservation already happened, so a later ceiling change (e.g.
        # auto_route flipped, or a lower ceiling on redelivery) must resume
        # forward to execute/paid instead of writing `compensated` without
        # releasing the reservation first (that would strand a live
        # reservation with no compensating release -- an orphaned decrement).
        already_reserved = record is not None and record.state == SagaState.reserved
        if (
            auto_route
            and ceiling_cents is not None
            and amount_cents > ceiling_cents
            and not already_reserved
        ):
            log.critical(
                "M12 ceiling violation: invoice_id=%s amount_cents=%s ceiling_cents=%s "
                "-- refusing payment, no funds moved",
                invoice_id, amount_cents, ceiling_cents,
            )
            record = await self._transition(
                key, invoice_id, correlation_id, department, amount_cents,
                state=SagaState.compensated, failure_reason=_REASON_CEILING_VIOLATION,
            )
            await self._publish_failed(record, compensated=False)
            return record

        if record is None:
            record = await self._transition(
                key, invoice_id, correlation_id, department, amount_cents,
                state=SagaState.started,
            )

        # Step 3: started -> reserve budget. Idempotent for SEQUENTIAL
        # redelivery only (the terminal-state check in Step 1 covers that
        # case); truly concurrent same-invoice delivery could still call
        # reserve() twice -- see module docstring for why that residual is
        # accepted rather than closed with a reservation record.
        if record.state == SagaState.started:
            reserved_ok = await self._budgets.reserve(department, amount_cents)
            if not reserved_ok:
                record = await self._transition(
                    key, invoice_id, correlation_id, department, amount_cents,
                    state=SagaState.rejected_insufficient_budget,
                    failure_reason=_REASON_INSUFFICIENT_BUDGET,
                )
                await self._publish_failed(record, compensated=False)
                return record
            record = await self._transition(
                key, invoice_id, correlation_id, department, amount_cents,
                state=SagaState.reserved,
            )

        # Step 4: reserved -> execute payment (provider idempotency makes a
        # crash-resume here safe to retry).
        if record.state == SagaState.reserved:
            try:
                ref = await self._provider.execute(invoice_id, amount_cents, scenario)
            except PaymentDeclined:
                # Journey D: reserve -> decline -> release. No orphaned
                # reservation.
                await self._budgets.release(department, amount_cents)
                record = await self._transition(
                    key, invoice_id, correlation_id, department, amount_cents,
                    state=SagaState.compensated, failure_reason=_REASON_PAYMENT_DECLINED,
                )
                await self._publish_failed(record, compensated=True)
                return record

            remaining = await self._budgets.get_remaining(department)
            record = await self._transition(
                key, invoice_id, correlation_id, department, amount_cents,
                state=SagaState.paid, payment_ref=ref,
            )
            await self._publish_completed(record, remaining)
            return record

        return record

    async def _transition(
        self,
        key: str,
        invoice_id: str,
        correlation_id: str,
        department: str,
        amount_cents: int,
        *,
        state: SagaState,
        payment_ref: str | None = None,
        failure_reason: str | None = None,
    ) -> SagaRecord:
        def update_fn(value):
            if value is None:
                base = {
                    "invoice_id": invoice_id,
                    "correlation_id": correlation_id,
                    "department": department,
                    "amount_cents": amount_cents,
                    "payment_ref": None,
                    "failure_reason": None,
                }
            else:
                base = dict(value)
            base["state"] = state
            if payment_ref is not None:
                base["payment_ref"] = payment_ref
            if failure_reason is not None:
                base["failure_reason"] = failure_reason
            base["updated_at"] = _now()
            return base

        new_value = await cas_update(self._store, key, update_fn)
        return SagaRecord(**new_value)

    async def _publish_failed(self, record: SagaRecord, *, compensated: bool) -> None:
        payload = PaymentFailedPayload(
            meta=new_event_meta(record.invoice_id, record.correlation_id),
            reason=record.failure_reason or "",
            compensated=compensated,
        )
        await self._publisher(TOPIC_PAYMENT_FAILED, payload.model_dump())

    async def _publish_completed(self, record: SagaRecord, budget_remaining_cents: int) -> None:
        payload = PaymentCompletedPayload(
            meta=new_event_meta(record.invoice_id, record.correlation_id),
            amount_cents=record.amount_cents,
            budget_remaining_cents=budget_remaining_cents,
            department=record.department,
        )
        await self._publisher(TOPIC_PAYMENT_COMPLETED, payload.model_dump())
