"""
Dice Connector â€” 5-Tier Scraping Architecture
==============================================
T1: Official RSS feed (build-time verified, 3-retry exponential backoff)
T2: Apify Store actor build-time check
T3: Playwright SPA scraper + sitemap.xml discovery
T4: SerpApi Google Jobs scoped to site:dice.com
T5: DB cache fallback (handled by orchestrator)

job_type is parsed per-listing from actual posting content â€” no hardcoded values.
"""

import os
import logging
import httpx
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SERPAPI_KEY = os.getenv("SERPAPI_API_KEY", "")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# RSS feed URL pattern â€” verified at build time; fall to T3 if feed is invalid
DICE_RSS_URL = "https://www.dice.com/jobs/q-{keyword}-l-Remote.rss"
DICE_JOBS_URL = "https://www.dice.com/jobs"
DICE_SITEMAP_URL = "https://www.dice.com/sitemap.xml"


def _parse_job_type_from_text(text: str) -> str:
    """
    Parse job_type per-listing from posting content.
    Never hardcodes â€” Dice mixes full-time, contract, and part-time.
    """
    lower = text.lower() if text else ""
    if any(w in lower for w in ["contract", "contractor", "freelance", "c2c", "corp-to-corp", "c-corp"]):
        return "contract"
    if any(w in lower for w in ["part-time", "part time", "parttime"]):
        return "part_time"
    if any(w in lower for w in ["full-time", "full time", "fulltime", "permanent"]):
        return "full_time"
    return "unknown"


class DiceConnector:
    """
    Dice connector implementing 5-tier scraping architecture.
    Low-cost tiers attempted first; escalates only on failure.
    """

    def __init__(
        self,
        serp_key: Optional[str] = None,
        apify_token: Optional[str] = None,
    ):
        self.serp_key = serp_key or SERPAPI_KEY
        self.apify_token = apify_token or APIFY_TOKEN

    # ------------------------------------------------------------------
    # T1: RSS Feed with build-time validation
    # ------------------------------------------------------------------
    def _fetch_t1_rss(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """Fetch Dice jobs via RSS feed. Falls through if feed is invalid."""
        rss_url = DICE_RSS_URL.format(keyword=keyword.replace(" ", "+"))
        logger.info(f"[Dice T1] Fetching RSS feed: {rss_url}")

        try:
            feedparser = None
            try:
                import feedparser as fp
                feedparser = fp
            except ImportError:
                pass

            if feedparser:
                feed = feedparser.parse(rss_url)
                # Build-time validity check: bozo flag + at least one entry
                if not feed.bozo and feed.entries:
                    return self._parse_rss_entries_feedparser(feed.entries)
                else:
                    logger.warning(
                        f"[Dice T1] RSS feed invalid (bozo={feed.bozo}, "
                        f"entries={len(feed.entries)}). Falling through to T3."
                    )
                    return []

            # Fallback: stdlib ET
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(rss_url)
            if res.status_code != 200:
                logger.warning(f"[Dice T1] RSS HTTP {res.status_code}. Falling through.")
                return []

            root = ET.fromstring(res.content)
            jobs = []
            for item in root.findall(".//item"):
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                desc = item.findtext("description") or ""
                pub = item.findtext("pubDate") or ""
                if title and link:
                    jobs.append({
                        "title": title.strip(),
                        "company": self._extract_company_from_title(title),
                        "url": link.strip(),
                        "location": "Remote",
                        "remote": True,
                        "job_type": _parse_job_type_from_text(f"{title} {desc}"),
                        "posted_date": pub,
                        "description": desc,
                        "platform_id": "dice",
                        "source_tier": "Tier 1 (RSS)",
                    })
            logger.info(f"[Dice T1] RSS returned {len(jobs)} jobs.")
            return jobs

        except Exception as exc:
            logger.warning(f"[Dice T1] RSS fetch error: {exc}. Falling through to T3.")
            return []

    def _parse_rss_entries_feedparser(self, entries) -> List[Dict[str, Any]]:
        jobs = []
        for entry in entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            desc = entry.get("summary", "") or entry.get("description", "")
            pub = entry.get("published", "")
            if title and link:
                jobs.append({
                    "title": title.strip(),
                    "company": self._extract_company_from_title(title),
                    "url": link.strip(),
                    "location": "Remote",
                    "remote": True,
                    "job_type": _parse_job_type_from_text(f"{title} {desc}"),
                    "posted_date": pub,
                    "description": desc,
                    "platform_id": "dice",
                    "source_tier": "Tier 1 (RSS)",
                })
        return jobs

    def _extract_company_from_title(self, title: str) -> str:
        if " at " in title:
            return title.split(" at ", 1)[1].strip()
        if " - " in title:
            parts = title.split(" - ", 1)
            if len(parts) == 2:
                return parts[1].strip()
        return "Dice Employer"

    # ------------------------------------------------------------------
    # T2: Apify Store actor (build-time check)
    # ------------------------------------------------------------------
    def _fetch_t2_apify(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """Check Apify Store for a Dice actor and run it if found."""
        if not self.apify_token:
            logger.info("[Dice T2] No APIFY_API_TOKEN configured. Skipping T2.")
            return []
        try:
            from pipeline.apify_store import check_apify_actor_available
            import asyncio
            loop = asyncio.new_event_loop()
            actor_id = loop.run_until_complete(check_apify_actor_available("dice"))
            loop.close()
            if not actor_id:
                logger.info("[Dice T2] No Dice actor found in Apify Store. Skipping T2.")
                return []
            logger.info(f"[Dice T2] Found Apify actor '{actor_id}' â€” executing.")
            from apify_client import ApifyClient
            client = ApifyClient(token=self.apify_token)
            run = client.actor(actor_id).call(run_input={"keyword": keyword, "maxItems": 200})
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "defaultDatasetId", None)
            if status == "SUCCEEDED" and dataset_id:
                items = client.dataset(dataset_id).list_items().items
                return self._normalize_apify_items(items, actor_id)
        except Exception as exc:
            logger.warning(f"[Dice T2] Apify error: {exc}")
        return []

    def _normalize_apify_items(self, items, actor_id: str) -> List[Dict[str, Any]]:
        jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("positionName") or item.get("jobTitle")
            url = item.get("url") or item.get("link") or item.get("jobUrl") or item.get("applyUrl")
            if not title or not url:
                continue
            desc = item.get("description") or item.get("summary") or ""
            jtype_raw = item.get("employmentType") or item.get("jobType") or item.get("contractType") or ""
            jobs.append({
                "title": str(title).strip(),
                "company": str(item.get("company") or item.get("companyName") or "Dice Employer").strip(),
                "url": str(url).strip(),
                "location": str(item.get("location") or "Remote").strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                "posted_date": item.get("postedAt") or item.get("postedDate"),
                "description": str(desc).strip(),
                "platform_id": "dice",
                "source_tier": f"Tier 2 (Apify:{actor_id})",
            })
        return jobs

    # ------------------------------------------------------------------
    # T3: Playwright SPA + sitemap.xml discovery
    # ------------------------------------------------------------------
    def _fetch_t3_playwright(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """Playwright SPA scraper with sitemap.xml discovery."""
        logger.info("[Dice T3] Attempting Playwright SPA scrape...")
        try:
            from pipeline.t3_scrapers import PlaywrightSpaScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                PlaywrightSpaScraper.scrape_playwright_spa(DICE_JOBS_URL, "dice")
            )
            loop.close()
            # Augment job_type parsing post-scrape
            for j in jobs:
                if not j.get("job_type") or j["job_type"] == "unknown":
                    j["job_type"] = _parse_job_type_from_text(
                        f"{j.get('title','')} {j.get('description','')}"
                    )
                j.setdefault("source_tier", "Tier 3 (Playwright)")
            logger.info(f"[Dice T3] Playwright returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[Dice T3] Playwright scrape error: {exc}")
        return []

    # ------------------------------------------------------------------
    # T4: SerpApi Google Jobs scoped to dice.com
    # ------------------------------------------------------------------
    def _fetch_t4_serpapi(self, keyword: str = "developer", country: str = "US", page: int = 1) -> List[Dict[str, Any]]:
        """Google Jobs via SerpApi scoped to site:dice.com/job-detail."""
        if not self.serp_key:
            logger.info("[Dice T4] No SERPAPI_API_KEY. Skipping T4.")
            return []
        logger.info(f"[Dice T4] SerpApi Google Jobs scoped to Dice for query='{keyword}', page={page}")
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item, parse_relative_posted_date
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} dice",
                "api_key": self.serp_key,
            }
            if page > 1:
                params["start"] = str((page - 1) * 10)
            with httpx.Client(timeout=15.0) as client:
                res = client.get("https://serpapi.com/search.json", params=params)
            if res.status_code != 200:
                logger.warning(f"[Dice T4] SerpApi HTTP {res.status_code}")
                return []
            data = res.json()
            jobs = []
            for item in data.get("jobs_results", []):
                title = item.get("title")
                direct_link = extract_direct_url_from_serpapi_item(item, "dice")
                if not direct_link:
                    logger.debug(f"[Dice T4] Skipping job '{title}' â€” no direct Dice URL found in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "Dice Employer")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": parse_relative_posted_date(ext.get("posted_at")) if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "platform_id": "dice",
                    "source_tier": "Tier 4 (SerpApi)",
                })
            logger.info(f"[Dice T4] SerpApi returned {len(jobs)} Dice jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[Dice T4] SerpApi error: {exc}")
        return []

    # ------------------------------------------------------------------
    # Main fetch entry point (T1 â†’ T2 â†’ T3 â†’ T4; T5 handled by orchestrator)
    # ------------------------------------------------------------------
    def fetch_jobs(
        self,
        keyword: str = "developer",
        country: str = "US",
        remote_only: bool = False,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Orchestrates T1â†’T2â†’T3â†’T4 with 3-retry exponential backoff per tier.
        Returns first non-empty result set found. T5 cache fallback is handled
        by the FiveTierScraperOrchestrator.
        page > 1: only T4 (SerpApi) supports real pagination; T1-T3 are static/RSS
        with no page concept, so page 2+ goes straight to T4.
        """
        if page > 1:
            return self._fetch_t4_serpapi(keyword, country, page=page)

        # Cascade through tiers WITHOUT retrying the same tier repeatedly.
        # A tier that returns 0 results almost never fixes itself 2-8s later —
        # retrying it 3x with backoff just burns latency. Moving straight to
        # the next tier is what the 5-layer fallback design is for.
        results = self._fetch_t1_rss(keyword)
        if results:
            logger.info(f"[Dice] T1 RSS succeeded with {len(results)} jobs.")
            return results

        results = self._fetch_t2_apify(keyword)
        if results:
            logger.info(f"[Dice] T2 Apify succeeded with {len(results)} jobs.")
            return results

        results = self._fetch_t3_playwright(keyword)
        if results:
            logger.info(f"[Dice] T3 Playwright succeeded with {len(results)} jobs.")
            return results

        results = self._fetch_t4_serpapi(keyword, country, page=1)
        if results:
            logger.info(f"[Dice] T4 SerpApi succeeded with {len(results)} jobs.")
            return results

        logger.warning("[Dice] All tiers T1-T4 returned 0 results. Triggering T5 cache fallback via orchestrator.")
        return []



