"""
Capability matrix — declares what each portal's CURRENT implementation
actually supports, derived from tracing the real connector code (not
assumed from portal names). Used by the date-filter and live-fetch logic
to decide whether a requested filter (e.g. "past 10 minutes") can be
honored accurately for a given portal, or whether that portal should be
excluded/flagged for that specific request instead of silently returning
inaccurate results.

freshness_precision values:
  "exact"          — source gives a real timestamp we can trust to the minute/hour
                      (e.g. USAJOBS PublicationStartDate, WWR RSS pubDate)
  "relative_text"  — source gives text like "3 days ago" which we parse via
                      connectors/serpapi_utils.parse_relative_posted_date;
                      accurate to the unit given, but not sub-unit precision
  "day_level"      — source rarely gives anything finer than a date
  "unknown"        — no reliable posted-date signal at all currently
"""

from typing import Dict, Any, Optional, List

PORTAL_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "linkedin": {
        "supports_keyword": True,
        "supports_country": True,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": True,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        # Firecrawl explicitly policy-blocks LinkedIn (confirmed via live
        # 403 "we do not support this site" response) -- not worth trying
        # as primary here; SerpApi Google Jobs aggregation remains reliable.
        "primary_method": "serpapi_google_jobs",
        "fallback_methods": ["brightdata", "apify_actor"],
        "firecrawl_unsupported": True,
    },
    "indeed": {
        "supports_keyword": True,
        "supports_country": True,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": True,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "serpapi_google_jobs", "apify_actor"],
    },
    "glassdoor": {
        "supports_keyword": True,
        "supports_country": True,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": True,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "serpapi_google_jobs", "apify_actor"],
    },
    "dice": {
        "supports_keyword": True,
        "supports_country": False,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "apify_actor", "rss", "serpapi_google_jobs"],
    },
    "ziprecruiter": {
        # PART 3: no Partner API key is configurable today, so the T1/T2 chain
        # can only ever fall through. Firecrawl + Playwright burn 15-20s and
        # return nothing, pushing the real SerpApi answer past the 30s race
        # window. Until a real key exists, go straight to SerpApi.
        "firecrawl_unsupported": True,
        "skip_playwright": True,
        "primary_method_override": "serpapi_google_jobs",
        "needs_api_key": "ZIPRECRUITER_PARTNER_API_KEY",
        "supports_keyword": True,
        "supports_country": False,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "apify_actor", "partner_api_pending", "serpapi_google_jobs"],
    },
    "usajobs": {
        "supports_keyword": True,
        "supports_country": False,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "exact",
        "supports_pagination": True,
        "supports_salary_filter": True,
        "supports_live_fetch": True,
        "primary_method": "official_api",
        "fallback_methods": ["serpapi_google_jobs"],
    },
    "careerbuilder": {
        # PART 3: same as ZipRecruiter -- no configurable API key, so the
        # pre-SerpApi tiers are pure latency cost with a zero success rate.
        "firecrawl_unsupported": True,
        "skip_playwright": True,
        "primary_method_override": "serpapi_google_jobs",
        "needs_api_key": "CAREERBUILDER_API_KEY",
        "supports_keyword": True,
        "supports_country": True,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "apify_actor", "developer_api_unconfirmed", "serpapi_google_jobs"],
    },
    "simplyhired": {
        "supports_keyword": True,
        "supports_country": True,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "relative_text",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "apify_actor", "serpapi_google_jobs"],
    },
    "weworkremotely": {
        "supports_keyword": True,
        "supports_country": False,
        "supports_remote_filter": True,
        "supports_date_filtering": True,
        "freshness_precision": "exact",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": True,
        "primary_method": "rss",
        "fallback_methods": ["serpapi_google_jobs"],
    },
    "hired": {
        "supports_keyword": True,
        "supports_country": False,
        "supports_remote_filter": False,
        "supports_date_filtering": False,
        "freshness_precision": "unknown",
        "supports_pagination": False,
        "supports_salary_filter": False,
        "supports_live_fetch": False,
        "primary_method": "firecrawl",
        "fallback_methods": ["brightdata", "apify_actor", "cache_primary", "serpapi_google_jobs"],
        "low_yield_platform": True,
    },
}


def get_capabilities(portal_id: str) -> Optional[Dict[str, Any]]:
    return PORTAL_CAPABILITIES.get((portal_id or "").lower().strip())


def portals_supporting(capability: str) -> List[str]:
    """Returns portal_ids where capability_key is truthy."""
    return [pid for pid, caps in PORTAL_CAPABILITIES.items() if caps.get(capability)]


def can_honor_freshness_window(portal_id: str, minutes: int) -> bool:
    """
    Returns whether this portal's current data source can reliably honor a
    freshness window this tight. Sub-hour windows (e.g. "past 10 minutes")
    require "exact" precision — relative_text sources like "X days ago"
    cannot be trusted below their smallest reported unit.
    """
    caps = get_capabilities(portal_id)
    if not caps or not caps.get("supports_date_filtering"):
        return False
    precision = caps.get("freshness_precision", "unknown")
    if precision == "exact":
        return True
    if precision == "relative_text":
        # Relative text sources (e.g. SerpApi "X hours ago") are reliable
        # down to roughly the hour level, not sub-hour precision.
        return minutes >= 60
    return False


def firecrawl_supported(portal_id: str) -> bool:
    """
    Whether Firecrawl should be ATTEMPTED for this portal at all.

    `firecrawl_unsupported` has been declared in this file for LinkedIn since
    it was written -- and was never read by any code path. Firecrawl was
    therefore attempted for LinkedIn on every portal fetch of every search,
    guaranteed to fail (LinkedIn returns a 403 "we do not support this site"),
    burning a key-rotation cycle and pushing all 5 keys toward cooldown for
    zero possible benefit. This function is what finally honours that flag.
    """
    caps = get_capabilities(portal_id) or {}
    return not caps.get("firecrawl_unsupported", False)


def playwright_supported(portal_id: str) -> bool:
    """Whether the Playwright SPA tier is worth attempting for this portal."""
    caps = get_capabilities(portal_id) or {}
    return not caps.get("skip_playwright", False)


def portals_needing_api_key() -> dict:
    """Portals that cannot work properly without a credential the user must obtain."""
    return {
        pid: caps["needs_api_key"]
        for pid, caps in PORTAL_CAPABILITIES.items()
        if caps.get("needs_api_key")
    }
