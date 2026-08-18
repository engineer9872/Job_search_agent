"""
TIER 0 -- THE FREE TIER.

Runs BEFORE any paid provider is touched. Costs nothing but bandwidth.

Three techniques, cheapest first, per portal:

  0a. NATIVE FEED     -- the portal's own free RSS/JSON API (USAJOBS official
                         API, WeWorkRemotely RSS, Dice RSS). Structured, exact
                         dates, no scraping, no key.
  0b. JSON-LD         -- schema.org/JobPosting embedded in the search page.
                         Boards publish this so Google Jobs can index them --
                         it is literally the same structured data SerpApi
                         resells back to us. scrapers/jsonld_harvester.py was
                         already written for this and was never wired into the
                         live path; this module finally connects it.
  0c. STATIC HTML     -- server-rendered job cards parsed with BeautifulSoup.

WHY THIS WAS RETURNING ZERO BEFORE
----------------------------------
The T3 dispatcher built its target URL as:

    f"https://{portal_id}.com"

which is `https://dice.com` -- a HOMEPAGE, with no keyword, no search, no job
cards. Every portal's free tier was fetching a marketing landing page and
correctly finding no jobs. This was never bot detection; it was the wrong URL.

Two further config bugs found and worked around here: `simplyhired` declares
strategy `playwright_cheerio` and `careerbuilder` declares
`cheerio_first_playwright_fallback`, neither of which the dispatcher's
if/elif chain knows about -- so both fell through to `return []` and never
dispatched at all.
"""

import re
import json
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus

import httpx

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from scrapers.jsonld_harvester import JSONLDHarvester

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# REAL search URLs. `{kw}` and `{loc}` are URL-encoded before formatting.
# Where a portal supports native date sorting we USE it -- a date-sorted page
# means the free tier's first page is the freshest page, which is exactly what
# the 12h/24h buckets need.
# ---------------------------------------------------------------------------
SEARCH_URLS: Dict[str, str] = {
    "linkedin": "https://www.linkedin.com/jobs/search?keywords={kw}&location={loc}&sortBy=DD&f_TPR={tpr}",
    "indeed": "https://www.indeed.com/jobs?q={kw}&l={loc}&sort=date&fromage={fromage}",
    "glassdoor": "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={kw}&locT=N&fromAge={fromage}",
    "dice": "https://www.dice.com/jobs?q={kw}&location={loc}&filters.postedDate={dice_age}",
    "ziprecruiter": "https://www.ziprecruiter.com/jobs-search?search={kw}&location={loc}&days={fromage}",
    "usajobs": "https://www.usajobs.gov/search/results/?k={kw}",
    "careerbuilder": "https://www.careerbuilder.com/jobs?keywords={kw}&location={loc}&posted={fromage}",
    "simplyhired": "https://www.simplyhired.com/search?q={kw}&l={loc}&t={fromage}",
    "weworkremotely": "https://weworkremotely.com/remote-jobs/search?term={kw}",
    "hired": "https://hired.com/jobs?query={kw}",
}

# Free, keyless, structured endpoints. Always tried FIRST where present.
NATIVE_FEEDS: Dict[str, Dict[str, str]] = {
    "weworkremotely": {"type": "rss", "url": "https://weworkremotely.com/remote-jobs.rss"},
    "dice": {"type": "rss", "url": "https://www.dice.com/jobs/rss?q={kw}"},
    "usajobs": {"type": "usajobs_api",
                "url": "https://data.usajobs.gov/api/search?Keyword={kw}&ResultsPerPage=50&SortField=DateAdded&SortDirection=Desc"},
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

BLOCK_SIGNATURES = [
    "just a moment", "checking your browser", "cf_chl", "datadome",
    "px-captcha", "perimeterx", "_abck", "unusual traffic", "access denied",
    "are you a robot", "enable javascript and cookies",
]

JOB_LINK_PATTERNS = [
    r'href="([^"]*/jobs?/view/[^"]*)"',
    r'href="([^"]*/job-detail/[^"]*)"',
    r'href="([^"]*/job/[^"]*)"',
    r'href="([^"]*viewjob\?jk=[^"]*)"',
    r'href="([^"]*/remote-jobs/[^"]*)"',
    r'href="([^"]*jobListingId=[^"]*)"',
]


class FreeTierResult:
    """Outcome of the free tier for one portal -- feeds the escalation ladder."""

    __slots__ = ("portal_id", "jobs", "method", "blocked", "status",
                 "elapsed", "error", "js_shell")

    def __init__(self, portal_id, jobs=None, method="none", blocked=None,
                 status=None, elapsed=0.0, error=None, js_shell=False):
        self.portal_id = portal_id
        self.jobs = jobs or []
        self.method = method
        self.blocked = blocked
        self.status = status
        self.elapsed = elapsed
        self.error = error
        self.js_shell = js_shell

    def to_dict(self):
        return {
            "portal_id": self.portal_id, "jobs_found": len(self.jobs),
            "method": self.method, "blocked": self.blocked, "status": self.status,
            "elapsed_s": round(self.elapsed, 2), "error": self.error,
            "js_shell": self.js_shell,
        }


def _hours_to_fromage(since_hours: Optional[int]) -> int:
    """Most boards express recency in DAYS. Sub-day windows clamp to 1."""
    if not since_hours:
        return 7
    return max(1, int(round(since_hours / 24.0)))


def _build_url(portal_id: str, keyword: str, location: str,
               since_hours: Optional[int]) -> Optional[str]:
    tmpl = SEARCH_URLS.get(portal_id)
    if not tmpl:
        return None
    fromage = _hours_to_fromage(since_hours)
    return tmpl.format(
        kw=quote_plus(keyword or "developer"),
        loc=quote_plus(location or ""),
        fromage=fromage,
        tpr=f"r{int(since_hours or 168) * 3600}",
        dice_age="ONE" if fromage <= 1 else ("SEVEN" if fromage <= 7 else "THIRTY"),
    )


def _detect_block(status: int, body: str) -> Optional[str]:
    head = (body or "")[:120000].lower()
    for sig in BLOCK_SIGNATURES:
        if sig in head:
            return sig
    if status in (403, 429):
        return f"http_{status}"
    return None


def _is_js_shell(html: str, found: int) -> bool:
    if found:
        return False
    txt = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return len(txt.split()) < 250


# ---------------------------------------------------------------------------
# 0a -- NATIVE FEEDS
# ---------------------------------------------------------------------------

def _fetch_native_feed(client: httpx.Client, portal_id: str,
                       keyword: str) -> List[Dict[str, Any]]:
    cfg = NATIVE_FEEDS.get(portal_id)
    if not cfg:
        return []
    url = cfg["url"].format(kw=quote_plus(keyword or "developer"))
    try:
        headers = {}
        if cfg["type"] == "usajobs_api":
            import os
            email = os.getenv("USAJOBS_EMAIL")
            key = os.getenv("USAJOBS_API_KEY")
            if not (email and key):
                return []
            headers = {"User-Agent": email, "Authorization-Key": key,
                       "Host": "data.usajobs.gov"}

        r = client.get(url, headers=headers or None)
        if r.status_code != 200:
            return []

        if cfg["type"] == "usajobs_api":
            data = r.json()
            items = (data.get("SearchResult", {}) or {}).get("SearchResultItems", []) or []
            out = []
            for it in items:
                d = (it or {}).get("MatchedObjectDescriptor", {}) or {}
                if d.get("PositionTitle") and d.get("PositionURI"):
                    out.append({
                        "title": d["PositionTitle"],
                        "company": (d.get("OrganizationName") or "US Government"),
                        "url": d["PositionURI"],
                        "location": ", ".join(
                            x.get("LocationName", "") for x in (d.get("PositionLocation") or [])[:2]
                        ),
                        # An EXACT publication timestamp -- no relative-text
                        # rounding, so pipeline/freshness.py can trust it fully.
                        "posted_date": d.get("PublicationStartDate"),
                        "posted_date_precision": "exact",
                        "description": (d.get("UserArea", {}).get("Details", {}) or {}).get("JobSummary", ""),
                        "platform_id": portal_id,
                        "source_tier": "Tier 0a (Native API)",
                    })
            return out

        # RSS
        out = []
        for m in re.finditer(r"<item>(.*?)</item>", r.text, flags=re.S | re.I):
            block = m.group(1)

            def pick(tag):
                mm = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
                               block, flags=re.S | re.I)
                return (mm.group(1).strip() if mm else "")

            title, link = pick("title"), pick("link")
            if not title or not link:
                continue
            company = title.split(":")[0].strip() if ":" in title else f"{portal_id.title()} Employer"
            out.append({
                "title": title.split(":", 1)[-1].strip() if ":" in title else title,
                "company": company,
                "url": link,
                "location": pick("region") or pick("category"),
                "posted_date": pick("pubDate"),
                "posted_date_precision": "exact",
                "description": re.sub(r"<[^>]+>", " ", pick("description"))[:600],
                "platform_id": portal_id,
                "source_tier": "Tier 0a (Native RSS)",
            })
        return out
    except Exception as e:
        logger.debug(f"[FreeTier:{portal_id}] Native feed failed: {e}")
        return []


# ---------------------------------------------------------------------------
# 0c -- STATIC HTML CARDS
# ---------------------------------------------------------------------------

def _parse_static_cards(html: str, portal_id: str, base_url: str) -> List[Dict[str, Any]]:
    if BeautifulSoup is None or not html:
        return []
    out, seen = [], set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out

    hrefs = set()
    for pat in JOB_LINK_PATTERNS:
        hrefs |= set(re.findall(pat, html, flags=re.I))

    for href in list(hrefs)[:120]:
        url = href if href.startswith("http") else (
            "https://" + base_url.split("/")[2] + (href if href.startswith("/") else "/" + href)
        )
        if url in seen:
            continue
        seen.add(url)

        title = ""
        try:
            anchor = soup.find("a", href=re.compile(re.escape(href[:80])))
            if anchor:
                title = anchor.get_text(strip=True) or (anchor.get("title") or "")
        except Exception:
            pass
        if not title or len(title) < 3:
            continue

        out.append({
            "title": title, "company": f"{portal_id.title()} Employer",
            "url": url, "location": "", "posted_date": None,
            "posted_date_precision": "unknown", "description": "",
            "platform_id": portal_id, "source_tier": "Tier 0c (Static HTML)",
        })
    return out


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def fetch_free_tier(portal_id: str, keyword: str = "developer", location: str = "",
                    since_hours: Optional[int] = None,
                    timeout: float = 12.0) -> FreeTierResult:
    """
    Runs 0a -> 0b -> 0c for one portal and stops at the first that yields jobs.
    NEVER raises -- a failure here just means the escalation ladder proceeds
    to a paid tier.
    """
    t0 = time.time()

    try:
        client = httpx.Client(headers=BROWSER_HEADERS, timeout=timeout,
                              follow_redirects=True, http2=True)
    except ImportError:
        client = httpx.Client(headers=BROWSER_HEADERS, timeout=timeout,
                              follow_redirects=True)

    with client:
        # ---- 0a: native feed --------------------------------------------
        feed_jobs = _fetch_native_feed(client, portal_id, keyword)
        if feed_jobs:
            logger.info(f"[FreeTier:{portal_id}] 0a native feed -> {len(feed_jobs)} jobs (FREE)")
            return FreeTierResult(portal_id, feed_jobs, "native_feed",
                                  elapsed=time.time() - t0, status=200)

        url = _build_url(portal_id, keyword, location, since_hours)
        if not url:
            return FreeTierResult(portal_id, [], "no_url", elapsed=time.time() - t0)

        try:
            r = client.get(url)
        except Exception as e:
            logger.info(f"[FreeTier:{portal_id}] fetch failed: {type(e).__name__}")
            return FreeTierResult(portal_id, [], "fetch_error",
                                  error=f"{type(e).__name__}: {e}",
                                  elapsed=time.time() - t0)

        body, status = r.text, r.status_code
        blocked = _detect_block(status, body)
        if blocked:
            logger.info(f"[FreeTier:{portal_id}] blocked ({blocked}) -> escalating")
            return FreeTierResult(portal_id, [], "blocked", blocked=blocked,
                                  status=status, elapsed=time.time() - t0)

        # ---- 0b: JSON-LD -------------------------------------------------
        try:
            jsonld_jobs = JSONLDHarvester().extract_job_postings_from_html(body, url)
        except Exception as e:
            logger.debug(f"[FreeTier:{portal_id}] JSON-LD parse failed: {e}")
            jsonld_jobs = []

        if jsonld_jobs:
            for j in jsonld_jobs:
                j.setdefault("platform_id", portal_id)
                j["source_tier"] = "Tier 0b (JSON-LD)"
                # JSON-LD datePosted is a real ISO date published by the board.
                j.setdefault("posted_date_precision", "day")
            logger.info(f"[FreeTier:{portal_id}] 0b JSON-LD -> {len(jsonld_jobs)} jobs (FREE)")
            return FreeTierResult(portal_id, jsonld_jobs, "jsonld",
                                  status=status, elapsed=time.time() - t0)

        # ---- 0c: static cards -------------------------------------------
        cards = _parse_static_cards(body, portal_id, url)
        if cards:
            logger.info(f"[FreeTier:{portal_id}] 0c static HTML -> {len(cards)} jobs (FREE)")
            return FreeTierResult(portal_id, cards, "static_html",
                                  status=status, elapsed=time.time() - t0)

        shell = _is_js_shell(body, 0)
        logger.info(
            f"[FreeTier:{portal_id}] no free data "
            f"({'JS shell' if shell else 'no cards found'}) -> escalating"
        )
        return FreeTierResult(portal_id, [], "empty", status=status,
                              js_shell=shell, elapsed=time.time() - t0)
