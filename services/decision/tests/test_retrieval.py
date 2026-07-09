"""Tests for the dependency-free policy retriever (N5.1).

Loads the real repo-root `policy.md` via the same `_find_repo_file` pattern
`deps.py` uses (see `services/decision/src/config.py::_find_repo_file`), so
these tests exercise the actual policy content, not a fixture copy.
"""

from services.decision.src.config import _find_repo_file
from services.decision.src.retrieval import CATEGORY_PREFIXES, PolicyRetriever, chunk_policy

_POLICY_MD = _find_repo_file("policy.md").read_text()

_KNOWN_IDS = {
    "MEAL-01",
    "MEAL-02",
    "MEAL-03",
    "TRAVEL-01",
    "TRAVEL-02",
    "TRAVEL-03",
    "SAAS-01",
    "HW-01",
    "HW-02",
}


def test_chunk_policy_finds_all_known_rule_ids():
    chunks = chunk_policy(_POLICY_MD)
    assert _KNOWN_IDS <= chunks.keys()
    global_ids = {rid for rid in chunks if rid.startswith("GLOBAL")}
    assert global_ids, "expected at least one GLOBAL-* rule id"


def test_chunk_policy_maps_rule_id_to_nonempty_text():
    chunks = chunk_policy(_POLICY_MD)
    for rid in _KNOWN_IDS:
        assert chunks[rid].strip(), f"{rid} has empty rule text"
    # spot-check actual content made it through, not just the id
    assert "75" in chunks["MEAL-01"]
    assert "alcohol" in chunks["MEAL-03"].lower()


def test_category_prefixes_cover_known_categories_and_include_global():
    for category in ("meals", "travel", "saas", "software", "hardware"):
        assert category in CATEGORY_PREFIXES
        assert "GLOBAL" in CATEGORY_PREFIXES[category]
    assert CATEGORY_PREFIXES["meals"][0] == "MEAL"
    assert CATEGORY_PREFIXES["travel"][0] == "TRAVEL"
    assert CATEGORY_PREFIXES["hardware"][0] == "HW"


def _retriever():
    return PolicyRetriever(chunk_policy(_POLICY_MD))


def test_retrieve_meals_invoice_includes_meal01_excludes_travel():
    invoice = {
        "category": "meals",
        "lineItems": [{"description": "Team lunch", "quantity": 1, "unitPrice": 38.89}],
        "notes": "Solo working lunch.",
    }
    selected = _retriever().retrieve(invoice)
    assert "MEAL-01" in selected
    assert not any(rid.startswith("TRAVEL") for rid in selected)


def test_retrieve_ranks_alcohol_line_item_into_meal03():
    invoice = {
        "category": "meals",
        "lineItems": [{"description": "Alcohol-only bar tab, wine and beer", "quantity": 1,
                        "unitPrice": 60.0}],
        "notes": "Alcohol-only receipt; exercising the reject route.",
    }
    selected = _retriever().retrieve(invoice, k=5)
    assert "MEAL-03" in selected


def test_retrieve_is_deterministic():
    invoice = {
        "category": "travel",
        "lineItems": [{"description": "Business class flight to Berlin", "quantity": 1,
                        "unitPrice": 2200.0}],
        "notes": "First class upgrade requested.",
    }
    r = _retriever()
    first = r.retrieve(invoice)
    second = r.retrieve(invoice)
    assert first == second


def test_render_returns_nonempty_string_with_selected_ids():
    retriever = _retriever()
    rendered = retriever.render(["MEAL-01", "MEAL-03"])
    assert rendered.strip()
    assert "MEAL-01" in rendered
    assert "MEAL-03" in rendered


def test_render_empty_list_is_empty_string():
    retriever = _retriever()
    assert retriever.render([]) == ""
