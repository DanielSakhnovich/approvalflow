"""Duplicate fingerprint registry with resubmission-owner exception (Gate 1)."""

import hashlib
from decimal import ROUND_HALF_UP, Decimal, DecimalException

from afcommon.state import try_register


def _to_cents_safe(value: object) -> int | None:
    """
    Convert a dollar-amount value to integer cents using ROUND_HALF_UP.
    Returns None strictly on parse failure (including non-finite values);
    a legitimate amount that rounds to 0 cents returns 0.
    """
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            return None
        return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError, TypeError, OverflowError):
        return None


def fingerprint_of(invoice: dict) -> str:
    """
    Compute sha256 fingerprint of invoice as: sha256(vendor|invoiceNumber|total_cents).

    Any parseable total (including one that rounds to 0 cents, e.g. 0.001 or
    0.004) maps deterministically through its cents value. Only a genuinely
    unparseable total falls back to the raw string of `total`, so malformed
    garbage still fingerprint-matches itself without crashing.
    """
    vendor = invoice.get("vendor", "")
    invoice_number = invoice.get("invoiceNumber", "")
    total = invoice.get("total")

    total_cents = _to_cents_safe(total)
    total_str = str(total_cents) if total_cents is not None else str(total)

    fingerprint_input = f"{vendor}|{invoice_number}|{total_str}"
    return hashlib.sha256(fingerprint_input.encode()).hexdigest()


class FingerprintRegistry:
    """
    Registry for duplicate detection with resubmission-owner exception.
    Uses atomic first-write-wins registration via try_register.
    """

    def __init__(self, store):
        """
        Args:
            store: StateStore with async get(key) and try_save(key, value, etag) methods
        """
        self.store = store

    async def check_and_register(self, invoice: dict, invoice_id: str) -> bool:
        """
        Check if invoice is a duplicate or resubmission.

        Returns:
            True if first sight OR same owner (resubmission)
            False if duplicate (different owner)
        """
        fp = fingerprint_of(invoice)
        key = f"fp:{fp}"
        value = {"invoiceId": invoice_id}

        # Try atomic registration
        registered = await try_register(self.store, key, value)

        if registered:
            # First registration succeeded
            return True

        # Registration failed (key exists) - check if same owner
        existing, _ = await self.store.get(key)
        if existing and existing.get("invoiceId") == invoice_id:
            # Same owner, this is a resubmission
            return True
        # Different owner -> duplicate. Note: `existing` being None here is
        # unreachable today (fp:* keys have no TTL/eviction, so a key that
        # just failed first-write registration must still be readable), but
        # if eviction ever appears we deliberately stay conservative and
        # treat the unreadable-owner case as a duplicate rather than risk a
        # double payment.
        return False
