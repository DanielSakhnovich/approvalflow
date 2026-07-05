"""Duplicate fingerprint registry with resubmission-owner exception (Gate 1)."""

import hashlib
from decimal import ROUND_HALF_UP, Decimal, DecimalException


def _to_cents_safe(value: object) -> int:
    """
    Convert a dollar-amount value to integer cents using ROUND_HALF_UP.
    On any parse error, return 0 (safe because fingerprint will use raw string fallback).
    """
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            return 0
        return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError, TypeError, OverflowError):
        return 0


def fingerprint_of(invoice: dict) -> str:
    """
    Compute sha256 fingerprint of invoice as: sha256(vendor|invoiceNumber|total_cents).

    For malformed total (unparseable), fall back to raw string of total.
    This ensures malformed garbage still fingerprint-matches itself.
    """
    vendor = invoice.get("vendor", "")
    invoice_number = invoice.get("invoiceNumber", "")
    total = invoice.get("total")

    # Try to convert total to cents; if it fails, use raw string
    total_cents = _to_cents_safe(total)
    if total_cents == 0:
        # Check if total was actually 0 or if it was malformed
        try:
            d = Decimal(str(total))
            if d.is_finite() and d == Decimal(0):
                # Legitimate zero
                total_str = "0"
            else:
                # Malformed or infinity, use raw string
                total_str = str(total)
        except (DecimalException, ValueError, TypeError):
            # Parse error, use raw string
            total_str = str(total)
    else:
        total_str = str(total_cents)

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
        from libs.afcommon.afcommon.state import try_register
        registered = await try_register(self.store, key, value)

        if registered:
            # First registration succeeded
            return True

        # Registration failed (key exists) - check if same owner
        existing, _ = await self.store.get(key)
        if existing and existing.get("invoiceId") == invoice_id:
            # Same owner, this is a resubmission
            return True
        else:
            # Different owner, duplicate
            return False
