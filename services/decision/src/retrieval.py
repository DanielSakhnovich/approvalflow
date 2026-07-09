"""N5.1: a small, dependency-free RAG retriever over `policy.md`.

No embeddings, no network calls. The policy is chunked one chunk per
`rule_id` by parsing the markdown tables in `policy.md`; retrieval for a
given invoice is a category-prefix filter (always-included rules for that
invoice's category, e.g. all `MEAL-*` + `GLOBAL-*` rules for a `meals`
invoice) unioned with the top-k BM25 keyword matches over the invoice's
line-item descriptions, notes, and category.

`chunk_policy` / `CATEGORY_PREFIXES` / `PolicyRetriever` are consumed by a
later task (N5.2) to wire this into the decision pipeline in place of
passing the full policy text to the agent -- the names and signatures here
are load-bearing and must not change.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Matches a markdown table row's first two cells where the first cell is a
# backtick-quoted rule id, e.g. "| `MEAL-01` | Personal/team meals ... |".
# The text-capture group excludes `|` so this only matches genuine 2-column
# rule rows -- it must NOT match 3+-column tables (e.g. the §6 "Autonomy
# thresholds" table, whose rows are `| key | default | meaning |`), which
# would otherwise leak pseudo-rule-ids like `AUTONOMY-CEILING`.
_RULE_ROW_RE = re.compile(
    r"^\|\s*`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`\s*\|\s*([^|]+?)\s*\|\s*$",
    re.MULTILINE,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Category (as seen on invoice fixtures) -> rule-id prefixes always
# included for that category. `GLOBAL` rules apply to every category.
CATEGORY_PREFIXES: dict[str, list[str]] = {
    "meals": ["MEAL", "GLOBAL"],
    "travel": ["TRAVEL", "GLOBAL"],
    "saas": ["SAAS", "GLOBAL"],
    "software": ["SAAS", "GLOBAL"],
    "hardware": ["HW", "GLOBAL"],
}


def chunk_policy(policy_md: str) -> dict[str, str]:
    """Parse `policy.md`'s markdown tables into rule_id -> rule text chunks.

    Every table row of the shape "| `RULE-ID` | rule text |" becomes one
    chunk keyed by RULE-ID, with the rule text (markdown emphasis/backticks
    left intact) as the chunk body. Rows are matched anywhere in the
    document, so this picks up every rule table (Meals, Travel, SaaS,
    Hardware, Global, ...) in one pass.
    """
    chunks: dict[str, str] = {}
    for match in _RULE_ROW_RE.finditer(policy_md):
        rule_id, text = match.group(1), match.group(2)
        chunks[rule_id] = text
    return chunks


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class _BM25:
    """Minimal BM25 (k1=1.5, b=0.75) over a small in-memory corpus.

    Private to this module -- N5.2 and beyond should go through
    `PolicyRetriever`, not this class directly.
    """

    k1 = 1.5
    b = 0.75

    def __init__(self, doc_ids: list[str], doc_texts: list[str]):
        self._doc_ids = doc_ids
        self._doc_tokens = [_tokenize(t) for t in doc_texts]
        self._doc_lens = [len(toks) for toks in self._doc_tokens]
        self._avgdl = (sum(self._doc_lens) / len(self._doc_lens)) if self._doc_lens else 0.0
        self._doc_freqs = [Counter(toks) for toks in self._doc_tokens]

        n_docs = len(self._doc_tokens)
        df: Counter[str] = Counter()
        for toks in self._doc_tokens:
            df.update(set(toks))
        self._idf: dict[str, float] = {
            term: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }

    def scores(self, query: str) -> dict[str, float]:
        """Score every doc against `query`; unseen terms contribute 0."""
        q_tokens = _tokenize(query)
        result: dict[str, float] = dict.fromkeys(self._doc_ids, 0.0)
        for i, doc_id in enumerate(self._doc_ids):
            doc_len = self._doc_lens[i]
            freqs = self._doc_freqs[i]
            score = 0.0
            for term in q_tokens:
                idf = self._idf.get(term)
                if idf is None:
                    continue
                tf = freqs.get(term, 0)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * doc_len / (self._avgdl or 1))
                score += idf * (tf * (self.k1 + 1)) / denom
            result[doc_id] = score
        return result


def _invoice_query_text(invoice: dict) -> str:
    parts: list[str] = []
    category = invoice.get("category")
    if category:
        parts.append(str(category))
    for item in invoice.get("lineItems") or []:
        desc = item.get("description") if isinstance(item, dict) else None
        if desc:
            parts.append(str(desc))
    notes = invoice.get("notes")
    if notes:
        parts.append(str(notes))
    return " ".join(parts)


class PolicyRetriever:
    """Retrieves the relevant `policy.md` rule chunks for an invoice.

    Construct with an already-chunked policy: `PolicyRetriever(chunk_policy(md))`.
    """

    def __init__(self, chunks: dict[str, str]):
        self._chunks = dict(chunks)
        self._rule_ids = sorted(self._chunks)  # deterministic base ordering
        self._bm25 = _BM25(self._rule_ids, [self._chunks[rid] for rid in self._rule_ids])

    def _category_ids(self, category: str | None) -> list[str]:
        prefixes = CATEGORY_PREFIXES.get((category or "").lower(), ["GLOBAL"])
        if "GLOBAL" not in prefixes:
            prefixes = [*prefixes, "GLOBAL"]
        return [
            rid for rid in self._rule_ids if any(rid.startswith(prefix) for prefix in prefixes)
        ]

    def retrieve(self, invoice: dict, k: int = 5) -> list[str]:
        """Select rule_ids for `invoice`: category-filtered ids unioned with
        the top-k BM25 matches for the invoice's query text. Ordering is
        deterministic: category ids first (sorted), then BM25 ids not
        already included, ranked by descending score with a sorted-id
        tie-break.
        """
        category_ids = self._category_ids(invoice.get("category"))
        selected = list(category_ids)
        selected_set = set(selected)

        query = _invoice_query_text(invoice)
        if query.strip() and k > 0:
            scores = self._bm25.scores(query)
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            top = [rid for rid, score in ranked if score > 0][:k]
            for rid in top:
                if rid not in selected_set:
                    selected.append(rid)
                    selected_set.add(rid)

        return selected

    def render(self, rule_ids: list[str]) -> str:
        """Render selected chunks as a policy-rules string for the agent
        prompt: one `rule_id: rule text` line per id, in the given order.
        Unknown ids are skipped. Empty input -> empty string.
        """
        lines = [f"{rid}: {self._chunks[rid]}" for rid in rule_ids if rid in self._chunks]
        return "\n".join(lines)
