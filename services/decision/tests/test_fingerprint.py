import asyncio
import json
from pathlib import Path

import pytest
from afcommon.state import InMemoryStateStore

from services.decision.src.fingerprint import FingerprintRegistry, fingerprint_of


def _fixtures() -> tuple:
    """Load sample invoices from repo root, same pattern as test_validators.py"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "sample-invoices.json").exists():
            data = json.loads((parent / "sample-invoices.json").read_text())
            return {f["id"]: f for f in data["fixtures"]}
    raise FileNotFoundError("sample-invoices.json not found")


FIXTURES = _fixtures()


def test_fingerprint_of_computes_hash():
    """fingerprint_of produces a consistent sha256 hex hash from vendor|invoiceNumber|total_cents"""
    inv = FIXTURES["INV-1001"]
    fp = fingerprint_of(inv)
    # Should be a 64-char hex string (sha256)
    assert isinstance(fp, str)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_of_same_invoice_same_hash():
    """Same invoice (INV-1001) produces consistent hash"""
    inv = FIXTURES["INV-1001"]
    fp1 = fingerprint_of(inv)
    fp2 = fingerprint_of(inv)
    assert fp1 == fp2


def test_fingerprint_of_inv1007_same_as_inv1001():
    """INV-1007 has same vendor|invoiceNumber|total as INV-1001, so same fingerprint"""
    fp1001 = fingerprint_of(FIXTURES["INV-1001"])
    fp1007 = fingerprint_of(FIXTURES["INV-1007"])
    assert fp1001 == fp1007


def test_fingerprint_near_zero_totals_are_deterministic():
    """Totals 0.001 and 0.004 both round to 0 cents -> IDENTICAL fingerprints."""
    inv_a = {**FIXTURES["INV-1001"], "total": 0.001}
    inv_b = {**FIXTURES["INV-1001"], "total": 0.004}
    assert fingerprint_of(inv_a) == fingerprint_of(inv_b)


def test_fingerprint_unparseable_total_self_matches_and_differs_from_zero():
    """A genuinely unparseable total (None) still fingerprint-matches itself
    and differs from a parseable total that rounds to 0 cents."""
    inv_none = {**FIXTURES["INV-1001"], "total": None}
    inv_zero = {**FIXTURES["INV-1001"], "total": 0.0}
    assert fingerprint_of(inv_none) == fingerprint_of(inv_none)
    assert fingerprint_of(inv_none) != fingerprint_of(inv_zero)


def test_fingerprint_of_different_totals_different_hash():
    """Different totals produce different fingerprints"""
    inv1 = FIXTURES["INV-1001"]
    inv2 = {**FIXTURES["INV-1001"], "total": 50.0}
    fp1 = fingerprint_of(inv1)
    fp2 = fingerprint_of(inv2)
    assert fp1 != fp2


@pytest.mark.asyncio
async def test_check_and_register_first_registration_true():
    """First registration of an invoice returns True (newly seen)"""
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)
    inv = FIXTURES["INV-1001"]
    result = await registry.check_and_register(inv, "inv-1001-id-1")
    assert result is True


@pytest.mark.asyncio
async def test_check_and_register_duplicate_different_owner_false():
    """
    INV-1007 (same fingerprint as INV-1001, different invoice_id)
    returns False when INV-1001 already registered (duplicate with different owner)
    """
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)

    # Register INV-1001 first
    inv1001 = FIXTURES["INV-1001"]
    result1 = await registry.check_and_register(inv1001, "inv-1001-id")
    assert result1 is True

    # Try to register INV-1007 (same fingerprint, different invoice_id)
    inv1007 = FIXTURES["INV-1007"]
    result2 = await registry.check_and_register(inv1007, "inv-1007-id")
    assert result2 is False  # Different owner detected


@pytest.mark.asyncio
async def test_check_and_register_same_owner_resubmission_true():
    """Re-submitting the same invoice (same invoice_id) returns True (resubmission/owner match)"""
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)
    inv = FIXTURES["INV-1001"]
    invoice_id = "inv-1001-id"

    # First registration
    result1 = await registry.check_and_register(inv, invoice_id)
    assert result1 is True

    # Re-submit same invoice with same id
    result2 = await registry.check_and_register(inv, invoice_id)
    assert result2 is True  # Same owner, allowed


@pytest.mark.asyncio
async def test_check_and_register_different_totals_both_true():
    """Invoices with different totals have different fingerprints, both register True"""
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)

    inv1 = FIXTURES["INV-1001"]  # total = 42.0
    inv2 = {**FIXTURES["INV-1001"], "total": 50.0}  # different total

    result1 = await registry.check_and_register(inv1, "id-1")
    assert result1 is True

    result2 = await registry.check_and_register(inv2, "id-2")
    assert result2 is True  # Different fingerprint, so both True


@pytest.mark.asyncio
async def test_check_and_register_concurrent_identical_one_wins():
    """
    Concurrent registration of identical invoices with different ids:
    exactly one succeeds (True), the other fails (False) because it sees the winner's id.
    The loser's comparison returns False (different owner).
    """
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)
    inv = FIXTURES["INV-1001"]

    # Two concurrent registrations with different invoice IDs
    task1 = registry.check_and_register(inv, "id-task-1")
    task2 = registry.check_and_register(inv, "id-task-2")

    results = await asyncio.gather(task1, task2)

    # Exactly one should be True, the other False
    assert results.count(True) == 1
    assert results.count(False) == 1


@pytest.mark.asyncio
async def test_check_and_register_malformed_total_fingerprint():
    """
    Malformed total (non-numeric) falls back to raw string in fingerprint.
    Malformed garbage still fingerprint-matches itself.
    """
    store = InMemoryStateStore()
    registry = FingerprintRegistry(store)

    inv_malformed = {
        **FIXTURES["INV-1001"],
        "total": "garbage-value"  # Malformed total
    }

    # First registration with malformed total
    result1 = await registry.check_and_register(inv_malformed, "id-1")
    assert result1 is True

    # Same malformed invoice with different id should be detected as duplicate
    result2 = await registry.check_and_register(inv_malformed, "id-2")
    assert result2 is False
