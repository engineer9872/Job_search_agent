"""
Firecrawl connector — PRIMARY data source for 8 of the 10 portals
(all except USAJOBS and We Work Remotely, which keep their existing
free official API/RSS as primary).

Uses Firecrawl's /v1/scrape endpoint with a JSON-schema-guided extraction
format: Firecrawl renders the target job-search page (handling JS, bot
detection, etc. server-side) and an LLM extracts structured job listings
matching our schema, directly from the live page content.

Supports multi-key rotation via config.settings.KeyRotator.
Raises ProviderAuthError / ProviderRateLimitError / ProviderUnavailableError
(not generic Exception) so KeyRotator's fallback logic can distinguish a
"provider failed" outcome from a "provider succeeded, zero results" outcome.
"""

import logging
import httpx
from typing import List, Dict, Any, Optional

from config.settings import (
    get_settings,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderCooldownError,
    ProviderUnsupportedSiteError,
)

logger = logging.getLogger(__name__)

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

# Portal search-page URL templates. {keyword} and {location} are URL-encoded
# by the caller before formatting. These are the actual public search pages
# for each portal — Firecrawl renders and reads them, it does not use any
# private/authenticated API.
PORTAL_SEARCH_URL_TEMPLATES: Dict[str, str] = {
    "linkedin": "https://www.linkedin.com/jobs/search/?keywords={keyword}&location={location}",
    "indeed": "https://www.indeed.com/jobs?q={keyword}&l={location}",
    "glassdoor": "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={keyword}&locT=&locId=",
    "dice": "https://www.dice.com/jobs?q={keyword}&location={location}",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/search?search={keyword}&location={location}",
    "careerbuilder": "https://www.careerbuilder.com/jobs?keywords={keyword}&location={location}",
    "simplyhired": "https://www.simplyhired.com/search?q={keyword}&l={location}",
    "hired": "https://hired.com/jobs?q={keyword}",
}

# JSON schema Firecrawl's extraction LLM is instructed to fill in from the
# rendered page. Kept deliberately close to our NormalizedJob raw-dict shape
# so downstream normalize.py needs no special-casing for Firecrawl-sourced jobs.
JOB_LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Exact job title as posted"},
                    "company": {"type": "string", "description": "Hiring company name"},
                    "url": {"type": "string", "description": "Direct URL to this specific job posting (not a search page)"},
                    "location": {"type": "string", "description": "City/region/country as shown, or 'Remote' if stated"},
                    "remote": {"type": "boolean", "description": "True if explicitly marked remote/work-from-home"},
                    "job_type": {"type": "string", "description": "Employment type as stated, e.g. Full-time, Contract, Part-time"},
                    "posted_date": {"type": "string", "description": "Posted date/time exactly as displayed on the page, e.g. '3 days ago', '2026-08-10'"},
                    "description": {"type": "string", "description": "Short description/snippet if visible on the listing"},
                },
                "required": ["title", "url"],
            },
        }
    },
    "required": ["jobs"],
}


def _classify_http_error(status_code: int, body_text: str) -> Exception:
    # Firecrawl policy-blocks certain sites (observed: LinkedIn) with a 403
    # and this specific message. This is permanent, not key/auth-related --
    # never trigger rotation or cooldown for it, just skip to next fallback.
    if status_code == 403 and "do not support this site" in body_text.lower():
        return ProviderUnsupportedSiteError(f"Firecrawl does not support this site (HTTP 403): {body_text[:200]}")
    if status_code == 401 or status_code == 403:
        return ProviderAuthError(f"Firecrawl auth failed (HTTP {status_code}): {body_text[:200]}")
    if status_code == 429:
        return ProviderRateLimitError(f"Firecrawl rate-limited (HTTP 429): {body_text[:200]}")
    if status_code >= 500:
        return ProviderUnavailableError(f"Firecrawl server error (HTTP {status_code}): {body_text[:200]}")
    return ProviderUnavailableError(f"Firecrawl request failed (HTTP {status_code}): {body_text[:200]}")


def _build_search_url(portal_id: str, keyword: str, location: str) -> Optional[str]:
    template = PORTAL_SEARCH_URL_TEMPLATES.get(portal_id.lower())
    if not template:
        return None
    import urllib.parse
    kw = urllib.parse.quote_plus(keyword or "developer")
    loc = urllib.parse.quote_plus(location or "")
    return template.format(keyword=kw, location=loc)


def _call_firecrawl_scrape(api_key: str, target_url: str, timeout: float = 90.0) -> Dict[str, Any]:
    """
    Single Firecrawl /v1/scrape call. Raises typed exceptions on failure so
    KeyRotator can decide whether to try the next key.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": target_url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": JOB_LISTING_SCHEMA,
            "prompt": (
                "Extract every job listing visible on this page. For each job, "
                "capture the exact title, company, a direct URL to that specific "
                "job posting (not a search/listing page), location, whether it is "
                "remote, the employment type, the posted date exactly as shown "
                "(do not reformat or guess), and a short description if present. "
                "If no jobs are visible, return an empty jobs array — do not invent listings."
            ),
        },
        "onlyMainContent": True,
        # Ask for the job-card region only rather than whole-page content.
        # Firecrawl bills by extracted content, so trimming this cuts credit
        # cost per call independently of any caching change.
        "includeTags": ["ul", "li", "article", "section", "a", "h2", "h3", "time", "span"],
        "excludeTags": ["nav", "footer", "header", "script", "style", "svg",
                        "iframe", "noscript", "form", "aside"],
        "removeBase64Images": True,
        "waitFor": 2000,
        # Bot-protected job boards (Dice, LinkedIn, Indeed, etc.) routinely fail
        # on Firecrawl's default proxy tier with ERR_TUNNEL_CONNECTION_FAILED.
        # "stealth" is Firecrawl's anti-bot-detection proxy tier — costs more
        # credits per Firecrawl's pricing, but is what these sites need.
        "proxy": "stealth",
    }

    # One retry: proxy-tier failures are frequently transient (a single bad
    # upstream connection), not a real "this key/provider is broken" signal.
    # Retrying with the SAME key before escalating to key rotation avoids
    # burning through configured keys for what's often a one-off network blip.
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.post(FIRECRAWL_SCRAPE_URL, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            last_exc = ProviderUnavailableError(f"Firecrawl request timed out: {e}")
            continue
        except httpx.RequestError as e:
            last_exc = ProviderUnavailableError(f"Firecrawl connection error: {e}")
            continue

        if res.status_code != 200:
            exc = _classify_http_error(res.status_code, res.text)
            # Only retry on 5xx (transient); auth/rate-limit errors should
            # propagate immediately so KeyRotator can move to the next key.
            if isinstance(exc, ProviderUnavailableError) and attempt == 0:
                last_exc = exc
                logger.info(f"[Firecrawl] Attempt {attempt+1} failed ({exc}); retrying once before rotating key.")
                continue
            raise exc

        last_exc = None
        break

    if last_exc is not None:
        raise last_exc

    try:
        data = res.json()
    except Exception as e:
        raise ProviderUnavailableError(f"Firecrawl returned non-JSON response: {e}")

    if not data.get("success", True):
        err = data.get("error", "unknown error")
        raise ProviderUnavailableError(f"Firecrawl reported failure: {err}")

    return data


def _extract_jobs_from_response(data: Dict[str, Any], portal_id: str) -> List[Dict[str, Any]]:
    """
    Parses the Firecrawl response into our standard raw-job-dict shape.
    Never fabricates fields — a missing url/title means the job is skipped.
    """
    result_data = data.get("data", {})
    json_payload = result_data.get("json", {}) if isinstance(result_data, dict) else {}
    raw_jobs = json_payload.get("jobs", []) if isinstance(json_payload, dict) else []

    parsed: List[Dict[str, Any]] = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url or not url.startswith("http"):
            continue
        parsed.append({
            "title": title,
            "company": str(item.get("company") or f"{portal_id.title()} Employer").strip(),
            "url": url,
            "location": str(item.get("location") or "").strip(),
            "remote": bool(item.get("remote", False)),
            "job_type": str(item.get("job_type") or "unknown").strip(),
            "posted_date": item.get("posted_date"),
            "description": str(item.get("description") or "").strip(),
            "platform_id": portal_id,
            "source_tier": "Primary (Firecrawl)",
        })
    return parsed



# ===========================================================================
# FIRECRAWL CALL BUDGET
# ===========================================================================
class FirecrawlCallBudget:
    """
    Hard ceiling on Firecrawl calls per hour and per day.

    Without this, a burst of searches fans out to ~8 eligible portals each and
    can exhaust every key in minutes -- after which EVERY subsequent portal
    fetch still pays the latency and log noise of walking all 5 keys only to
    find them all cooling down. Once the budget is spent we skip straight to
    the next tier with a single log line.

    Env overrides: FIRECRAWL_HOURLY_BUDGET, FIRECRAWL_DAILY_BUDGET.
    """

    def __init__(self):
        import os as _os
        self.hourly_limit = int(_os.getenv("FIRECRAWL_HOURLY_BUDGET", "40"))
        self.daily_limit = int(_os.getenv("FIRECRAWL_DAILY_BUDGET", "300"))
        self._lock = __import__("threading").Lock()
        self._hour_bucket = None
        self._day_bucket = None
        self._hour_count = 0
        self._day_count = 0
        self.skipped_by_budget = 0
        self._reserve_used = 0

    def _roll(self):
        import time as _t
        now = _t.time()
        h = int(now // 3600)
        d = int(now // 86400)
        if self._hour_bucket != h:
            self._hour_bucket, self._hour_count = h, 0
        if self._day_bucket != d:
            self._day_bucket, self._day_count = d, 0

    def try_consume(self, portal_id: str = "") -> tuple:
        with self._lock:
            self._roll()
            if self._hour_count >= self.hourly_limit:
                self.skipped_by_budget += 1
                return False, (f"hourly Firecrawl budget spent "
                               f"({self._hour_count}/{self.hourly_limit}); using fallback tiers.")
            if self._day_count >= self.daily_limit:
                self.skipped_by_budget += 1
                return False, (f"daily Firecrawl budget spent "
                               f"({self._day_count}/{self.daily_limit}); using fallback tiers.")
            self._hour_count += 1
            self._day_count += 1
            return True, "ok"

    def reserve_available(self) -> bool:
        """
        A small extra allowance, on top of the daily cap, reserved exclusively
        for portals whose free tier was blocked. Sized at 25% of the daily cap
        so it can never become the main spending path.
        """
        with self._lock:
            self._roll()
            reserve_cap = max(5, int(self.daily_limit * 0.25))
            if self._reserve_used >= reserve_cap:
                return False
            self._reserve_used += 1
            return True

    def status(self) -> dict:
        with self._lock:
            self._roll()
            return {
                "hourly_used": self._hour_count,
                "hourly_limit": self.hourly_limit,
                "daily_used": self._day_count,
                "daily_limit": self.daily_limit,
                "calls_skipped_by_budget": self.skipped_by_budget,
                "blocked_portal_reserve_used": self._reserve_used,
                "blocked_portal_reserve_cap": max(5, int(self.daily_limit * 0.25)),
            }


firecrawl_budget = FirecrawlCallBudget()


def fetch_jobs_via_firecrawl(
    portal_id: str,
    keyword: str = "developer",
    location: str = "",
    bypass_budget: bool = False,
) -> List[Dict[str, Any]]:
    """
    Main entry point. Returns [] if Firecrawl is not configured at all
    (caller should treat this as "not attempted", not "failed") or if the
    provider was reachable but genuinely found zero jobs.
    Raises ProviderUnavailableError only when ALL configured keys failed —
    callers use this to decide whether to proceed to the next fallback method.
    """
    settings = get_settings()

    # ---- GATE 1: portals that are KNOWN to reject Firecrawl -------------
    # `firecrawl_unsupported` was declared in pipeline/capabilities.py but read
    # by nothing, so LinkedIn (which returns 403 "we do not support this site")
    # was attempted on every fetch of every search -- a guaranteed failure that
    # burned a full 5-key rotation cycle each time and drove every key into
    # cooldown for no possible benefit.
    from pipeline.capabilities import firecrawl_supported
    if not firecrawl_supported(portal_id):
        logger.info(
            f"[Firecrawl:{portal_id}] Skipped: this portal is known to reject Firecrawl. "
            f"Not attempted, not counted against quota."
        )
        return []

    # ---- GATE 2: call budget --------------------------------------------
    # `bypass_budget` is set when the free tier came back BLOCKED. A bot-walled
    # page cannot be reached by SerpApi or a plain HTTP fetch, so Firecrawl is
    # the only source that will return anything at all -- that call is worth
    # more than the budget it costs. A reserve is still kept so a run of
    # blocked portals cannot drain the daily allowance entirely.
    allowed, budget_msg = firecrawl_budget.try_consume(portal_id)
    if not allowed:
        if bypass_budget and firecrawl_budget.reserve_available():
            logger.info(
                f"[Firecrawl:{portal_id}] Budget spent, but the free tier was BLOCKED "
                f"for this portal -- using reserve allowance."
            )
        else:
            logger.info(f"[Firecrawl:{portal_id}] Skipped: {budget_msg}")
            return []

    if not settings.firecrawl.is_configured():
        logger.info(f"[Firecrawl:{portal_id}] Not configured (no API key). Skipping.")
        return []

    target_url = _build_search_url(portal_id, keyword, location)
    if not target_url:
        logger.warning(f"[Firecrawl:{portal_id}] No search URL template defined for this portal. Skipping.")
        return []

    logger.info(f"[Firecrawl:{portal_id}] Scraping live search page: {target_url}")

    def _attempt(api_key: str) -> List[Dict[str, Any]]:
        data = _call_firecrawl_scrape(api_key, target_url)
        jobs = _extract_jobs_from_response(data, portal_id)
        return jobs

    try:
        jobs = settings.firecrawl.call_with_rotation(_attempt)
        logger.info(f"[Firecrawl:{portal_id}] SUCCESS: extracted {len(jobs)} job(s) from live page.")
        return jobs
    except ProviderCooldownError as e:
        # Distinct from a real failure: every key is deliberately cooling
        # down after a recent error, nothing was actually attempted here.
        logger.info(f"[Firecrawl:{portal_id}] Skipped (cooldown after recent failure): {e}")
        raise
    except ProviderUnavailableError as e:
        logger.warning(f"[Firecrawl:{portal_id}] All configured key(s) failed: {e}")
        raise
    except ValueError:
        return []
