"""
Dynamic, generic keyword parsing and matching (Filter Accuracy Plan v2).
Nothing here is tied to a fixed job-title list -- works for any text the
user types, every time.
"""

import re
from typing import List, Dict, Optional, Tuple

MODIFIER_WORDS = {
    "senior", "sr", "junior", "jr", "lead", "principal", "staff",
    "i", "ii", "iii", "iv", "v",
    "entry", "entry-level", "mid", "mid-level", "associate",
    "intern", "internship", "trainee",
}

STOPWORDS = {"a", "an", "the", "of", "for", "in", "and", "or", "to", "with", "on"}


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"[a-zA-Z0-9\+\#\.\-]+", text.lower())
    return [t.strip("-.") for t in tokens if t and t not in STOPWORDS and t.strip("-.")]


def classify_tokens(tokens: List[str]) -> Tuple[List[str], List[str]]:
    required, optional = [], []
    for t in tokens:
        if t in MODIFIER_WORDS:
            optional.append(t)
        else:
            required.append(t)
    return required, optional


def parse_search_terms(raw_terms: List[str]) -> List[Dict[str, List[str]]]:
    """
    Turns free-text terms into term-groups, each with its own
    keywords_required / keywords_optional. Empty terms, or terms with only
    modifier words, are skipped (they carry no matchable signal).
    """
    groups = []
    for term in raw_terms:
        term = (term or "").strip()
        if not term:
            continue
        tokens = tokenize(term)
        required, optional = classify_tokens(tokens)
        if required:
            groups.append({"keywords_required": required, "keywords_optional": optional})
    return groups


def job_matches_any_term_group(
    title: Optional[str],
    canonical_title: Optional[str],
    term_groups: List[Dict[str, List[str]]],
) -> bool:
    """
    Step 5a: AND-match within a term's required keywords, OR-match across
    multiple terms. If term_groups is empty (no title/keyword filter given
    at all), every job passes -- unspecified filters are never enforced.
    """
    if not term_groups:
        return True

    haystacks = [h for h in [(title or "").lower(), (canonical_title or "").lower()] if h]
    if not haystacks:
        return False

    for group in term_groups:
        required = group["keywords_required"]
        if all(
            any(re.search(r"\b" + re.escape(kw) + r"\b", h) for h in haystacks)
            for kw in required
        ):
            return True

    return False


def build_combined_scrape_keyword(term_groups: List[Dict[str, List[str]]]) -> str:
    """
    Builds one search-query string to send to a portal's own keyword search
    -- a superset covering all required keywords across all term groups.
    The strict per-job filtering in job_matches_any_term_group() is what
    actually enforces accuracy; this string only needs to bring back a
    broad enough candidate set.
    """
    seen = []
    for group in term_groups:
        for kw in group["keywords_required"]:
            if kw not in seen:
                seen.append(kw)
    return " ".join(seen) if seen else "developer"
