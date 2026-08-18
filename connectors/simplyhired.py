"""
SimplyHired Connector â€” 5-Tier Scraping Architecture
===================================================
T1: No confirmed public API â€” marked unavailable
T2: Apify Store actor build-time check
T3: Playwright/Cheerio scraper + sitemap.xml discovery
T4: SerpApi Google Jobs scoped to site:simplyhired.com
    NOTE: Expect overlap with Indeed source (same parent company, Indeed Inc.).
    Rely on existing dedup hash logic (title + company + platform_id + posted_date)
    rather than artificially separating these two sources.
T5: DB cache fallback (handled by orchestrator)

job_type is parsed per-listing from actual posting content â€” no hardcoded values.
"""

import os
import logging
import httpx
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SERPAPI_KEY = os.getenv("SERPAPI_API_KEY", "")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN", "")

SIMPLYHIRED_JOBS_URL = "https://www.simplyhired.com/search"
SIMPLYHIRED_SITEMAP_URL = "https://www.simplyhired.com/sitemap.xml"


def _parse_job_type_from_text(text: str) -> str:
    """
    Parse job_type per-listing from posting content.
    SimplyHired mixes full-time, contract, and part-time â€” never hardcode.
    """
    lower = text.lower() if text else ""
    if any(w in lower for w in ["contract", "contractor", "freelance", "c2c", "corp-to-corp", "c-corp", "temp"]):
        return "contract"
    if any(w in lower for w in ["part-time", "part time", "parttime"]):
        return "part_time"
    if any(w in lower for w in ["full-time", "full time", "fulltime", "permanent"]):
        return "full_time"
    return "unknown"


class SimplyHiredConnector:
    """
    SimplyHired connector implementing 5-tier scraping architecture.

    T1 is unavailable (no confirmed public API). Primary active path is T2/T3/T4.

    Overlap with Indeed is expected (same parent company). The existing
    dedup hash logic in the pipeline handles deduplication â€” this connector
    does NOT attempt to filter Indeed-sourced listings from SimplyHired results.
    """

    def __init__(
        self,
        serp_key: Optional[str] = None,
        apify_token: Optional[str] = None,
    ):
        self.serp_key = serp_key or SERPAPI_KEY
        self.apify_token = apify_token or APIFY_TOKEN

    # ------------------------------------------------------------------
    # T1: No confirmed public API â€” unavailable
    # ------------------------------------------------------------------
    def _fetch_t1(self) -> List[Dict[str, Any]]:
        """T1 is unavailable for SimplyHired â€” no confirmed public API."""
        logger.info("[SimplyHired T1] No confirmed public API â€” T1 unavailable. Proceeding to T2.")
        return []

    # ------------------------------------------------------------------
    # T2: Apify Store actor (build-time check)
    # ------------------------------------------------------------------
    def _fetch_t2_apify(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        if not self.apify_token:
            logger.info("[SimplyHired T2] No APIFY_API_TOKEN. Skipping T2.")
            return []
        try:
            from pipeline.apify_store import check_apify_actor_available
            import asyncio
            loop = asyncio.new_event_loop()
            actor_id = loop.run_until_complete(check_apify_actor_available("simplyhired"))
            loop.close()
            if not actor_id:
                logger.info("[SimplyHired T2] No SimplyHired actor in Apify Store. Skipping T2.")
                return []
            logger.info(f"[SimplyHired T2] Found actor '{actor_id}' â€” executing.")
            from apify_client import ApifyClient
            client = ApifyClient(token=self.apify_token)
            run = client.actor(actor_id).call(run_input={"keyword": keyword, "maxItems": 200})
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "defaultDatasetId", None)
            if status == "SUCCEEDED" and dataset_id:
                items = client.dataset(dataset_id).list_items().items
                return self._normalize_apify_items(items, actor_id)
        except Exception as exc:
            logger.warning(f"[SimplyHired T2] Apify error: {exc}")
        return []

    def _normalize_apify_items(self, items, actor_id: str) -> List[Dict[str, Any]]:
        jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("jobTitle") or item.get("positionName")
            url = item.get("url") or item.get("link") or item.get("jobUrl") or item.get("applyUrl")
            if not title or not url:
                continue
            desc = item.get("description") or item.get("summary") or ""
            jtype_raw = item.get("employmentType") or item.get("jobType") or item.get("contractType") or ""
            jobs.append({
                "title": str(title).strip(),
                "company": str(item.get("company") or item.get("companyName") or "SimplyHired Employer").strip(),
                "url": str(url).strip(),
                "location": str(item.get("location") or "Remote").strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                "posted_date": item.get("postedAt") or item.get("postedDate"),
                "description": str(desc).strip(),
                "platform_id": "simplyhired",
                "source_tier": f"Tier 2 (Apify:{actor_id})",
            })
        return jobs

    # ------------------------------------------------------------------
    # T3: Playwright/Cheerio + sitemap.xml
    # ------------------------------------------------------------------
    def _fetch_t3_playwright_cheerio(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """Attempt static scrape first; escalate to Playwright if needed."""
        logger.info("[SimplyHired T3] Attempting static scrape...")
        try:
            from pipeline.t3_scrapers import StaticCheerioScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                StaticCheerioScraper.scrape_static_html(
                    SIMPLYHIRED_JOBS_URL, "simplyhired", SIMPLYHIRED_SITEMAP_URL
                )
            )
            loop.close()
            if jobs:
                for j in jobs:
                    if not j.get("job_type") or j["job_type"] == "unknown":
                        j["job_type"] = _parse_job_type_from_text(
                            f"{j.get('title','')} {j.get('description','')}"
                        )
                    j.setdefault("source_tier", "Tier 3 (Static Scrape)")
                logger.info(f"[SimplyHired T3] Static scrape returned {len(jobs)} jobs.")
                return jobs
        except Exception as exc:
            logger.info(f"[SimplyHired T3] Static scrape failed ({exc}). Escalating to Playwright.")

        try:
            from pipeline.t3_scrapers import PlaywrightSpaScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                PlaywrightSpaScraper.scrape_playwright_spa(SIMPLYHIRED_JOBS_URL, "simplyhired")
            )
            loop.close()
            for j in jobs:
                if not j.get("job_type") or j["job_type"] == "unknown":
                    j["job_type"] = _parse_job_type_from_text(
                        f"{j.get('title','')} {j.get('description','')}"
                    )
                j.setdefault("source_tier", "Tier 3 (Playwright)")
            logger.info(f"[SimplyHired T3] Playwright returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[SimplyHired T3] Playwright error: {exc}")
        return []

    # ------------------------------------------------------------------
    # T4: SerpApi Google Jobs (overlap with Indeed expected â€” let dedup handle it)
    # ------------------------------------------------------------------
    def _fetch_t4_serpapi(self, keyword: str = "developer", country: str = "US", page: int = 1) -> List[Dict[str, Any]]:
        if not self.serp_key:
            logger.info("[SimplyHired T4] No SERPAPI_API_KEY. Skipping T4.")
            return []
        logger.info(
            f"[SimplyHired T4] SerpApi Google Jobs for query='{keyword}', page={page}. "
            "NOTE: Overlap with Indeed expected (same parent company) â€” relying on dedup hash logic."
        )
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} simplyhired",
                "api_key": self.serp_key,
            }
            if page > 1:
                params["start"] = str((page - 1) * 10)
            with httpx.Client(timeout=15.0) as client:
                res = client.get("https://serpapi.com/search.json", params=params)
            if res.status_code != 200:
                return []
            jobs = []
            for item in res.json().get("jobs_results", []):
                title = item.get("title")
                direct_link = extract_direct_url_from_serpapi_item(item, "simplyhired")
                if not direct_link:
                    logger.debug(f"[SimplyHired T4] Skipping job '{title}' â€” no direct SimplyHired URL in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "SimplyHired Employer")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "platform_id": "simplyhired",
                    "source_tier": "Tier 4 (SerpApi)",
                })
            logger.info(f"[SimplyHired T4] SerpApi returned {len(jobs)} jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[SimplyHired T4] SerpApi error: {exc}")
        return []

    # ------------------------------------------------------------------
    # Main fetch entry point
    # ------------------------------------------------------------------
    def fetch_jobs(
        self,
        keyword: str = "developer",
        country: str = "US",
        remote_only: bool = False,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """Orchestrates T1(skip)â†’T2â†’T3â†’T4. T5 cache handled by orchestrator.
        page > 1: only T4 supports real pagination."""
        import time
        if page > 1:
            return self._fetch_t4_serpapi(keyword, country, page=page)

        # T1 always returns [] (no public API)
        self._fetch_t1()

        for attempt in range(1, 4):
            results = self._fetch_t2_apify(keyword)
            if results:
                return results
            if attempt < 3:
                time.sleep(2 ** attempt)

        for attempt in range(1, 4):
            results = self._fetch_t3_playwright_cheerio(keyword)
            if results:
                return results
            if attempt < 3:
                time.sleep(2 ** attempt)

        for attempt in range(1, 4):
            results = self._fetch_t4_serpapi(keyword, country, page=1)
            if results:
                return results
            if attempt < 3:
                time.sleep(2 ** attempt)

        logger.warning("[SimplyHired] All tiers T2â€“T4 returned 0 results. Triggering T5 via orchestrator.")
        return []



