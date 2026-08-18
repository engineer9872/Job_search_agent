"""
Query relevance scoring.

WHY THIS EXISTS
---------------
The pipeline has always measured success as "how many rows came back". That is
the wrong metric and it is why paid APIs get burned on searches that already
had good answers, while searches that returned 40 irrelevant rows never
escalate.

A search for "cloud engineer" that returns 40 rows of "Cloud Sales Executive"
is a FAILED search. A search that returns 6 rows of genuine Cloud Engineer
roles is a SUCCESSFUL one. The escalation ladder in pipeline/escalation.py
spends money based on MATCH YIELD -- how many jobs actually satisfy what the
user asked for -- not raw count.

SCORING
-------
Each job gets 0.0-1.0 against the parsed query:

  1.00  exact phrase present in title            ("cloud engineer" in title)
  0.85  all query tokens present in title, any order
  0.60  most query tokens in title + a domain-synonym hit
  0.40  all tokens present, but only in company/skills/description
  0.15  weak/partial overlap
  0.00  no meaningful overlap

`is_exact_match` is score >= EXACT_MATCH_THRESHOLD. Only these count toward
match yield, so escalation is driven by real answers, not filler.
"""

import re
import logging
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)

EXACT_MATCH_THRESHOLD = 0.60

# Common job-title equivalences. Kept deliberately small and hand-checked --
# a sloppy synonym list is how "engineer" starts matching "sales engineer".
SYNONYM_GROUPS = [
    {"engineer", "developer", "programmer", "swe"},
    {"ml", "machine learning", "ai", "artificial intelligence"},
    {"devops", "sre", "site reliability", "platform engineer"},
    {"frontend", "front end", "front-end", "ui engineer"},
    {"backend", "back end", "back-end", "server side"},
    {"fullstack", "full stack", "full-stack"},
    {"qa", "quality assurance", "test automation", "sdet"},
    {"data scientist", "data science"},
    {"cybersecurity", "security engineer", "infosec", "appsec"},
    {"cloud", "aws", "azure", "gcp"},
    {"pm", "product manager", "product management"},
]

# Tokens that carry no discriminating power in a job title.
STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "at", "to", "with",
    "job", "jobs", "role", "roles", "position", "positions", "opening",
    "openings", "hiring", "career", "careers", "opportunity", "remote",
    "senior", "junior", "sr", "jr", "lead", "principal", "staff", "i", "ii",
    "iii", "iv", "entry", "level", "mid", "experienced",
}

# Titles that superficially contain engineering words but are a different job.
# Without these, "cloud engineer" happily matches "Cloud Sales Executive".
NEGATIVE_SIGNALS = {
    "sales", "recruiter", "recruiting", "account executive", "business development",
    "marketing", "customer success", "support representative", "intern",
    "internship", "volunteer", "unpaid",
}


def _norm(text: Any) -> str:
    if not text:
        return ""
    t = str(text).lower()
    t = re.sub(r"[^a-z0-9+#. ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t and t not in STOPWORDS]


def _synonym_expand(tokens: List[str]) -> set:
    expanded = set(tokens)
    joined = " ".join(tokens)
    for group in SYNONYM_GROUPS:
        if any(term in joined or term in expanded for term in group):
            expanded |= group
    return expanded


def parse_query(query: str) -> Dict[str, Any]:
    """Turns raw user query text into the structure score_job() consumes."""
    phrases = [p.strip() for p in (query or "").split(",") if p.strip()]
    if not phrases:
        phrases = [query] if query else []

    parsed = []
    for p in phrases:
        toks = _tokens(p)
        if not toks:
            continue
        parsed.append({
            "phrase": _norm(p),
            "tokens": toks,
            "expanded": _synonym_expand(toks),
        })
    return {"groups": parsed, "raw": query or ""}


def score_job(job: Dict[str, Any], parsed_query: Dict[str, Any]) -> Tuple[float, str]:
    """
    Returns (score, reason). An empty query scores 1.0 -- the user asked for
    everything, so everything matches.
    """
    groups = parsed_query.get("groups") or []
    if not groups:
        return 1.0, "no_query_all_match"

    title = _norm(job.get("title") or job.get("canonical_title"))
    title_tokens = set(_tokens(title))
    body = _norm(" ".join(str(job.get(f) or "") for f in
                          ("company", "skills", "description_snippet", "description")))
    body_tokens = set(_tokens(body))

    # A negative signal in the TITLE caps the score below the exact-match bar.
    # It does not zero the job out -- "Sales Engineer" is a real engineering
    # role for some users -- it just stops it counting as an exact match.
    has_negative = any(neg in title for neg in NEGATIVE_SIGNALS)

    best, best_reason = 0.0, "no_overlap"

    for g in groups:
        phrase, toks, expanded = g["phrase"], set(g["tokens"]), g["expanded"]

        if phrase and phrase in title:
            score, reason = 1.0, "exact_phrase_in_title"
        elif toks and toks.issubset(title_tokens):
            score, reason = 0.85, "all_tokens_in_title"
        elif toks and len(toks & title_tokens) / len(toks) >= 0.5 and (expanded & title_tokens):
            score, reason = 0.60, "majority_tokens_plus_synonym_in_title"
        elif toks and toks.issubset(title_tokens | body_tokens):
            score, reason = 0.40, "all_tokens_but_only_in_body"
        elif toks and (toks & title_tokens):
            score, reason = 0.15, "partial_title_overlap"
        else:
            score, reason = 0.0, "no_overlap"

        if has_negative and score >= EXACT_MATCH_THRESHOLD:
            score, reason = 0.35, f"{reason}_but_negative_title_signal"

        if score > best:
            best, best_reason = score, reason

    return best, best_reason


def is_exact_match(job: Dict[str, Any], parsed_query: Dict[str, Any]) -> bool:
    return score_job(job, parsed_query)[0] >= EXACT_MATCH_THRESHOLD


def evaluate_batch(jobs: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """
    Scores a batch and returns the yield metrics the escalation ladder uses
    to decide whether spending money is justified.
    """
    parsed = parse_query(query)
    scored = []
    for j in jobs:
        s, r = score_job(j, parsed)
        scored.append((s, r, j))

    exact = [x for x in scored if x[0] >= EXACT_MATCH_THRESHOLD]
    total = len(scored)

    return {
        "total": total,
        "exact_matches": len(exact),
        "match_yield": round(len(exact) / total, 3) if total else 0.0,
        "avg_score": round(sum(x[0] for x in scored) / total, 3) if total else 0.0,
        "scored": scored,
    }


def rank_jobs(jobs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """
    Sorts by relevance first, recency second, and stamps each job with its
    score so the UI can show WHY a result ranked where it did.
    """
    parsed = parse_query(query)
    enriched = []
    for j in jobs:
        s, r = score_job(j, parsed)
        j = dict(j)
        j["_relevance_score"] = round(s, 3)
        j["_relevance_reason"] = r
        j["_is_exact_match"] = s >= EXACT_MATCH_THRESHOLD
        enriched.append(j)

    def _recency_key(j):
        return str(j.get("posted_date") or j.get("scraped_at") or j.get("fetched_at") or "")

    enriched.sort(key=lambda j: (j["_relevance_score"], _recency_key(j)), reverse=True)
    return enriched
