"""
D-016: the mechanical consumer-dedupe wrapper, extracted once into afcommon.

Every Dapr pub/sub consumer in this system (intake today; decision-svc,
approval, payment, audit, notification later) needs the exact same three
mechanical steps around an inbound event: unwrap the CloudEvent envelope's
`data` field into a typed payload, bind the correlation/invoice logging
contextvars from that payload's `meta`, and atomically check-and-mark
`processed:{event_id}` so a redelivered event has no second effect (M10).

This module owns exactly those three steps and nothing more -- handler
*bodies* (counters, publishing, sagas) stay per-service. Dedupe state stays
per-service automatically: each service's Dapr state store applies its own
app-id key prefix (D-015), so sharing this code never shares data.
"""

from typing import Any

from pydantic import BaseModel

from .logging import correlation_id_var, invoice_id_var
from .state import StateStore, try_register


class EventDedupe:
    """Atomic first-time-only event processing marker, keyed by event_id."""

    def __init__(self, store: StateStore):
        self._store = store

    async def first_time(self, event_id: str) -> bool:
        """True the first time `event_id` is seen, False on every redelivery."""
        return await try_register(self._store, f"processed:{event_id}", {"seen": True})


def bind_event_context(meta: Any) -> None:
    """
    Set the correlation_id/invoice_id logging contextvars from `meta`.

    `meta` may be anything carrying `correlation_id`/`invoice_id` -- an
    `afcommon.events.EventMeta`, another pydantic model with those fields, or
    a plain dict with those two keys -- so callers aren't forced to
    construct an `EventMeta` just to bind context.
    """
    if isinstance(meta, dict):
        correlation_id = meta["correlation_id"]
        invoice_id = meta["invoice_id"]
    else:
        correlation_id = meta.correlation_id
        invoice_id = meta.invoice_id
    correlation_id_var.set(correlation_id)
    invoice_id_var.set(invoice_id)


def parse_cloudevent[ModelT: BaseModel](model: type[ModelT], body: dict) -> ModelT:
    """
    Unwrap a Dapr CloudEvent envelope's `data` field and validate it into
    `model`.

    Raises `ValueError` if `body` has no `data` field: a malformed envelope
    from the sidecar is a configuration bug, not a normal runtime condition
    a handler should silently paper over.
    """
    if "data" not in body:
        raise ValueError("cloudevent body missing 'data' field")
    return model.model_validate(body["data"])
