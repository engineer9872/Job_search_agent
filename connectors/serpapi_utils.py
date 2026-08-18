"""
Shared SerpApi utility: extract direct job-posting URLs from the
`apply_options` / `related_links` arrays returned by Google Jobs engine.
"""

import re
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def extract_direct_url_from_serpapi_item(
    item: Dict[str, Any],
    platform_id: str,
) -> Optional[str]:
    """
    Given one SerpApi `jobs_results[]` item and a target platform_id,
    search through `apply_options` and `related_links` for a direct
    job-posting URL on that platform.  Returns the URL string or None.

    Priority order:
      1. apply_options[].link  (direct apply links provided by Google Jobs)
      2. related_links[].link  (related links to source platforms)
      3. share_link            (Google Jobs share link — last resort, but still
                                may contain a redirect to the original)
    """
    platform_domains = {
        "linkedin": ["linkedin.com/jobs", "linkedin.com/in/", "linkedin.com/comm/jobs", "linkedin.com"],
        "indeed": ["indeed.com/viewjob", "indeed.com/rc/clk", "indeed.com/job", "indeed.com/m/", "indeed."],
        "glassdoor": ["glassdoor.com/job-listing", "glassdoor.com/partner/jobListing", "glassdoor.com/job", "glassdoor."],
        "dice": ["dice.com/job-detail/", "dice.com/jobs/detail", "dice.com"],
        "ziprecruiter": ["ziprecruiter.com/jobs", "ziprecruiter.com/c/", "ziprecruiter.com/job", "ziprecruiter."],
        "usajobs": ["usajobs.gov/job/", "usajobs.gov/GetJob/", "usajobs.gov"],
        "careerbuilder": ["careerbuilder.com/job-details", "careerbuilder.com/job", "careerbuilder.com/jobs", "careerbuilder."],
        "simplyhired": ["simplyhired.com/job", "simplyhired.com/view", "simplyhired."],
        "weworkremotely": ["weworkremotely.com/remote-jobs", "weworkremotely.com"],
        "hired": ["hired.com/jobs", "hired.com/co", "hired.com"],
    }

    domain_needles = platform_domains.get(platform_id.lower(), [])

    # 1. Check apply_options first (most reliable for direct links)
    for opt in item.get("apply_options", []):
        link = (opt.get("link") or "").strip()
        if not link:
            continue
        link_lower = link.lower()
        # If we have domain needles, prefer a link that matches the target platform
        if domain_needles:
            for needle in domain_needles:
                if needle in link_lower:
                    return link
        # If no domain needles, accept any non-Google link
        elif "google.com" not in link_lower:
            return link

    # 2. Check related_links (older SerpApi field)
    for rl in item.get("related_links", []):
        link = (rl.get("link") or "").strip()
        if not link:
            continue
        link_lower = link.lower()
        if domain_needles:
            for needle in domain_needles:
                if needle in link_lower:
                    return link
        elif "google.com" not in link_lower:
            return link

    # 3. Fall back to any apply_options link that is NOT a google.com search link
    for opt in item.get("apply_options", []):
        link = (opt.get("link") or "").strip()
        if link and "google.com" not in link.lower():
            return link

    return None


def build_serpapi_params(
    keyword: str,
    platform_id: str,
    location: str = "",
    serp_key: str = "",
    page: int = 1,
    date_posted: str = "",
) -> Dict[str, str]:
    """
    Build SerpApi Google Jobs query params with optional native date filtering.
    """
    q = f"{keyword} {platform_id}"
    params = {
        "engine": "google_jobs",
        "q": q,
        "api_key": serp_key,
    }
    if location:
        params["location"] = location

    if page > 1:
        params["start"] = str((page - 1) * 10)

    # Native date filtering via chips parameter
    if date_posted:
        dp = date_posted.lower()
        if dp in ["past_24h", "24h", "today", "past 24 hours"]:
            params["chips"] = "date_posted:today"
        elif dp in ["past_week", "week", "7d", "past week"]:
            params["chips"] = "date_posted:week"
        elif dp in ["past_month", "month", "30d", "past month"]:
            params["chips"] = "date_posted:month"

    return params


import re
from datetime import datetime, timedelta, timezone


def parse_relative_posted_date(text) -> str | None:
    """
    Converts SerpApi/Google Jobs relative date strings (e.g. '30+ days ago',
    '3 days ago', '1 hour ago', 'Just posted', '2 weeks ago') into a real
    ISO 8601 UTC timestamp. Returns None if the text can't be parsed —
    callers should treat None as "unknown", never as "recent".
    """
    if not text or not isinstance(text, str):
        return None

    t = text.strip().lower()
    now = datetime.now(timezone.utc)

    if "just posted" in t or "today" in t:
        return now.isoformat()

    match = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s*ago", t)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == "minute":
        delta = timedelta(minutes=amount)
    elif unit == "hour":
        delta = timedelta(hours=amount)
    elif unit == "day":
        delta = timedelta(days=amount)
    elif unit == "week":
        delta = timedelta(weeks=amount)
    elif unit == "month":
        delta = timedelta(days=amount * 30)
    else:
        return None

    return (now - delta).isoformat()
