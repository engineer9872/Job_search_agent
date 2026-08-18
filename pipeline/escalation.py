"""
THE ESCALATION LADDER.

One rule: money is spent only when the FREE tier failed to answer the user's
actual question -- and "answer" is measured in EXACT MATCHES, not row count.

    Tier 0  FREE      native feed -> JSON-LD -> static HTML     cost 0
              |
              |  gate: enough exact matches?  --> YES: stop. Spend nothing.
              v  NO
    Tier 1  SerpApi   ~1 call/portal, fast (1-3s), cheapest paid option
              |
              |  gate: enough exact matches now?  --> YES: stop.
              v  NO
    Tier 2  Apify     heavy actor, 60-90s, most expensive per run.
              |       Only for portals with a REAL actor (Indeed) and only
              |       when SerpApi under-delivered.
              v  NO
    Tier 3  Firecrawl LAST. Budget-capped, skipped for portals that reject it.

WHY THIS ORDER (and not the one that was running)
-------------------------------------------------
The previous design raced Firecrawl against everything on EVERY request, and
put SerpApi at tier 4 behind three tiers that were broken or unimplemented.
Net effect: the most expensive provider ran first and unconditionally, and the
cheapest reliable one ran last. This inverts that.

Cost per portal per search, measured against observed behaviour:
    Tier 0  $0
    Tier 1  ~1 SerpApi call
    Tier 2  ~$0.02/run + 60-90s  (Indeed actor, maxItems=300)
    Tier 3  Firecrawl credits, stealth proxy tier

WHY MATCH YIELD AND NOT ROW COUNT
---------------------------------
40 rows of "Cloud Sales Executive" for a "cloud engineer" query is a failed
search that the old count-based logic treated as a success. 6 genuine Cloud
Engineer roles is a good search the old logic would have escalated on. The gate
uses pipeline/relevance.py so spend tracks answer quality.
"""

import time
import logging
from typing import List, Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)

# Enough exact matches to consider a portal answered. Below this we escalate.
MIN_EXACT_MATCHES = 5

# If the free tier already returned this many exact matches, never escalate
# even if the raw count looks small -- the question is answered.
SATISFIED_EXACT_MATCHES = 12

# Below this match yield the results are mostly noise, so escalate even if the
# raw count is high (this is the "40 irrelevant rows" case).
MIN_MATCH_YIELD = 0.25


class LadderStats:
    """Per-search accounting -- what each tier cost and what it bought."""

    def __init__(self):
        self.tiers_run: List[str] = []
        self.free_jobs = 0
        self.serpapi_calls = 0
        self.apify_runs = 0
        self.firecrawl_calls = 0
        self.portals_answered_free: List[str] = []
        self.portals_escalated: List[str] = []
        self.per_portal: Dict[str, Any] = {}
        self.started = time.time()

    def to_dict(self) -> Dict[str, Any]:
        total_portals = len(self.per_portal) or 1
        return {
            "elapsed_s": round(time.time() - self.started, 2),
            "tiers_run": self.tiers_run,
            "free_jobs": self.free_jobs,
            "paid_calls": {
                "serpapi": self.serpapi_calls,
                "apify": self.apify_runs,
                "firecrawl": self.firecrawl_calls,
            },
            "portals_answered_free": self.portals_answered_free,
            "portals_escalated": self.portals_escalated,
            "free_coverage_pct": round(
                len(self.portals_answered_free) / total_portals * 100, 1
            ),
            "per_portal": self.per_portal,
        }


def should_escalate(jobs: List[Dict[str, Any]], query: str,
                    stage: str = "free") -> tuple:
    """
    Returns (escalate: bool, reason: str, metrics: dict).

    An empty query never escalates on relevance -- with nothing to match
    against, every row is a valid answer and spending money would be pointless.
    """
    from pipeline.relevance import evaluate_batch

    m = evaluate_batch(jobs, query)
    exact, yield_ = m["exact_matches"], m["match_yield"]

    if not (query or "").strip():
        if len(jobs) >= MIN_EXACT_MATCHES:
            return False, f"{stage}_no_query_sufficient_rows", m
        return True, f"{stage}_no_query_too_few_rows", m

    if exact >= SATISFIED_EXACT_MATCHES:
        return False, f"{stage}_satisfied_{exact}_exact_matches", m

    if exact >= MIN_EXACT_MATCHES and yield_ >= MIN_MATCH_YIELD:
        return False, f"{stage}_sufficient_{exact}_matches_yield_{yield_}", m

    if exact < MIN_EXACT_MATCHES:
        return True, f"{stage}_only_{exact}_exact_matches_need_{MIN_EXACT_MATCHES}", m

    return True, f"{stage}_low_yield_{yield_}_below_{MIN_MATCH_YIELD}", m


def run_escalation_ladder(
    portal_id: str,
    keyword: str,
    location: str,
    query_text: str,
    since_hours: Optional[int],
    stats: LadderStats,
    serpapi_fn: Optional[Callable] = None,
    apify_fn: Optional[Callable] = None,
    firecrawl_fn: Optional[Callable] = None,
    allow_paid: bool = True,
) -> List[Dict[str, Any]]:
    """
    Runs the full ladder for ONE portal and returns the accumulated jobs.

    Each tier's output is ADDED to what came before, not substituted -- a
    portal that yields 3 free matches and 4 SerpApi matches should return
    all 7, not throw the free 3 away.
    """
    from pipeline.free_tier import fetch_free_tier
    from pipeline.capabilities import firecrawl_supported

    collected: List[Dict[str, Any]] = []
    portal_log: Dict[str, Any] = {"tiers": [], "free_method": None, "blocked": None}

    # ---------------- TIER 0 : FREE -------------------------------------
    t0 = fetch_free_tier(portal_id, keyword, location, since_hours)
    collected.extend(t0.jobs)
    stats.free_jobs += len(t0.jobs)
    portal_log["free_method"] = t0.method
    portal_log["blocked"] = t0.blocked
    portal_log["tiers"].append({"tier": "0_free", "method": t0.method,
                                "jobs": len(t0.jobs), "elapsed_s": round(t0.elapsed, 2)})
    if "tier0_free" not in stats.tiers_run:
        stats.tiers_run.append("tier0_free")

    escalate, reason, metrics = should_escalate(collected, query_text, "free")
    portal_log["after_free"] = {"exact": metrics["exact_matches"],
                                "yield": metrics["match_yield"], "decision": reason}

    if not escalate:
        logger.info(
            f"[Ladder:{portal_id}] ANSWERED FREE via {t0.method} -- "
            f"{metrics['exact_matches']} exact matches. Zero paid calls."
        )
        stats.portals_answered_free.append(portal_id)
        stats.per_portal[portal_id] = portal_log
        return collected

    if not allow_paid:
        logger.info(f"[Ladder:{portal_id}] Escalation needed ({reason}) but paid tiers disabled.")
        stats.per_portal[portal_id] = portal_log
        return collected

    stats.portals_escalated.append(portal_id)
    logger.info(f"[Ladder:{portal_id}] Escalating to paid: {reason}")

    # ---------------- TIER 1 : SerpApi (cheapest paid) -------------------
    if serpapi_fn:
        try:
            jobs = serpapi_fn(portal_id, keyword, location, since_hours) or []
            collected.extend(jobs)
            stats.serpapi_calls += 1
            portal_log["tiers"].append({"tier": "1_serpapi", "jobs": len(jobs)})
            if "tier1_serpapi" not in stats.tiers_run:
                stats.tiers_run.append("tier1_serpapi")

            escalate, reason, metrics = should_escalate(collected, query_text, "serpapi")
            portal_log["after_serpapi"] = {"exact": metrics["exact_matches"],
                                           "yield": metrics["match_yield"],
                                           "decision": reason}
            if not escalate:
                logger.info(
                    f"[Ladder:{portal_id}] Answered at SerpApi -- "
                    f"{metrics['exact_matches']} exact. Apify/Firecrawl skipped."
                )
                stats.per_portal[portal_id] = portal_log
                return collected
        except Exception as e:
            logger.warning(f"[Ladder:{portal_id}] SerpApi tier failed: {e}")

    # ---------------- TIER 2 : Apify (heavy, expensive) ------------------
    if apify_fn:
        try:
            jobs = apify_fn(portal_id, keyword, location, since_hours) or []
            if jobs:
                collected.extend(jobs)
                stats.apify_runs += 1
                portal_log["tiers"].append({"tier": "2_apify", "jobs": len(jobs)})
                if "tier2_apify" not in stats.tiers_run:
                    stats.tiers_run.append("tier2_apify")

                escalate, reason, metrics = should_escalate(collected, query_text, "apify")
                portal_log["after_apify"] = {"exact": metrics["exact_matches"],
                                             "yield": metrics["match_yield"],
                                             "decision": reason}
                if not escalate:
                    logger.info(f"[Ladder:{portal_id}] Answered at Apify. Firecrawl skipped.")
                    stats.per_portal[portal_id] = portal_log
                    return collected
        except Exception as e:
            logger.warning(f"[Ladder:{portal_id}] Apify tier failed: {e}")

    # ---------------- TIER 3 : Firecrawl (last, budget-capped) -----------
    if firecrawl_fn and firecrawl_supported(portal_id):
        try:
            jobs = firecrawl_fn(portal_id, keyword, location, since_hours) or []
            if jobs:
                collected.extend(jobs)
                stats.firecrawl_calls += 1
                portal_log["tiers"].append({"tier": "3_firecrawl", "jobs": len(jobs)})
                if "tier3_firecrawl" not in stats.tiers_run:
                    stats.tiers_run.append("tier3_firecrawl")
        except Exception as e:
            logger.warning(f"[Ladder:{portal_id}] Firecrawl tier failed: {e}")
    elif firecrawl_fn:
        logger.info(f"[Ladder:{portal_id}] Firecrawl skipped -- portal rejects it.")

    stats.per_portal[portal_id] = portal_log
    return collected
