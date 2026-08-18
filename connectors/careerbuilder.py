"""
CareerBuilder Connector â€” 5-Tier Scraping Architecture
=====================================================
T1: Developer API integration point (build-time check; falls to T3 if unavailable)
T2: Apify Store actor build-time check
T3: Cheerio-style static scrape first (cheaper), Playwright fallback; sitemap.xml discovery
T4: SerpApi Google Jobs scoped to site:careerbuilder.com
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
CAREERBUILDER_API_KEY = os.getenv("CAREERBUILDER_API_KEY", "")

CAREERBUILDER_JOBS_URL = "https://www.careerbuilder.com/jobs"
CAREERBUILDER_SITEMAP_URL = "https://www.careerbuilder.com/sitemap.xml"
CAREERBUILDER_API_BASE = "https://api.careerbuilder.com/consumer/jobsearch"


def _parse_job_type_from_text(text: str) -> str:
    """
    Parse job_type per-listing from posting content.
    CareerBuilder mixes full-time, contract, and part-time â€” never hardcode.
    """
    lower = text.lower() if text else ""
    if any(w in lower for w in ["contract", "contractor", "freelance", "c2c", "corp-to-corp", "c-corp", "temp"]):
        return "contract"
    if any(w in lower for w in ["part-time", "part time", "parttime"]):
        return "part_time"
    if any(w in lower for w in ["full-time", "full time", "fulltime", "permanent"]):
        return "full_time"
    return "unknown"


class CareerBuilderConnector:
    """
    CareerBuilder connector implementing 5-tier scraping architecture.
    T3 uses Cheerio-style static scrape first; escalates to Playwright only
    if content is not present in the initial HTML response.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        serp_key: Optional[str] = None,
        apify_token: Optional[str] = None,
    ):
        self.api_key = api_key or CAREERBUILDER_API_KEY
        self.serp_key = serp_key or SERPAPI_KEY
        self.apify_token = apify_token or APIFY_TOKEN

    # ------------------------------------------------------------------
    # T1: Developer API (build-time check)
    # ------------------------------------------------------------------
    def _fetch_t1_developer_api(self, keyword: str = "developer", country: str = "US") -> List[Dict[str, Any]]:
        """
        CareerBuilder developer API â€” historically free-tier available.
        Build-time check: if no API key is configured, log warning and fall to T3.
        """
        if not self.api_key:
            logger.warning(
                "[CareerBuilder T1] CAREERBUILDER_API_KEY not configured. "
                "Check current self-serve availability at developer.careerbuilder.com. "
                "Falling through to T3."
            )
            return []
        try:
            logger.info(f"[CareerBuilder T1] Querying developer API for keyword='{keyword}'")
            headers = {"Authorization": f"Bearer {self.api_key}"}
            params = {
                "keywords": keyword,
                "country_code": country,
                "hits_per_page": 50,
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.get(CAREERBUILDER_API_BASE, headers=headers, params=params)
            if res.status_code != 200:
                logger.warning(f"[CareerBuilder T1] API HTTP {res.status_code}. Falling to T3.")
                return []
            data = res.json()
            jobs = []
            for item in data.get("ResponseJobSearch", {}).get("Results", []):
                title = item.get("JobTitle", "")
                url = item.get("JobDetailsURL", "")
                if not title or not url:
                    continue
                desc = item.get("DescriptionTeaser", "")
                jtype_raw = item.get("EmploymentType", "")
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("Company", "CareerBuilder Employer")).strip(),
                    "url": str(url).strip(),
                    "location": str(item.get("Location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("Location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": item.get("PostedDate"),
                    "description": str(desc).strip(),
                    "platform_id": "careerbuilder",
                    "source_tier": "Tier 1 (Developer API)",
                })
            logger.info(f"[CareerBuilder T1] Developer API returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[CareerBuilder T1] API error: {exc}. Falling to T3.")
            return []

    # ------------------------------------------------------------------
    # T2: Apify Store actor (build-time check)
    # ------------------------------------------------------------------
    def _fetch_t2_apify(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        if not self.apify_token:
            logger.info("[CareerBuilder T2] No APIFY_API_TOKEN. Skipping T2.")
            return []
        try:
            from pipeline.apify_store import check_apify_actor_available
            import asyncio
            loop = asyncio.new_event_loop()
            actor_id = loop.run_until_complete(check_apify_actor_available("careerbuilder"))
            loop.close()
            if not actor_id:
                logger.info("[CareerBuilder T2] No CareerBuilder actor in Apify Store. Skipping T2.")
                return []
            logger.info(f"[CareerBuilder T2] Found actor '{actor_id}' â€” executing.")
            from apify_client import ApifyClient
            client = ApifyClient(token=self.apify_token)
            run = client.actor(actor_id).call(run_input={"keyword": keyword, "maxItems": 200})
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "defaultDatasetId", None)
            if status == "SUCCEEDED" and dataset_id:
                items = client.dataset(dataset_id).list_items().items
                return self._normalize_apify_items(items, actor_id)
        except Exception as exc:
            logger.warning(f"[CareerBuilder T2] Apify error: {exc}")
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
                "company": str(item.get("company") or item.get("companyName") or "CareerBuilder Employer").strip(),
                "url": str(url).strip(),
                "location": str(item.get("location") or "Remote").strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                "posted_date": item.get("postedAt") or item.get("postedDate"),
                "description": str(desc).strip(),
                "platform_id": "careerbuilder",
                "source_tier": f"Tier 2 (Apify:{actor_id})",
            })
        return jobs

    # ------------------------------------------------------------------
    # T3: Cheerio-style static scrape first, Playwright fallback
    # ------------------------------------------------------------------
    def _fetch_t3_cheerio_playwright(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """
        Attempt sitemap.xml discovery + static HTTP scrape first (cheaper).
        Escalate to Playwright only if content is not in initial HTML response.
        """
        logger.info("[CareerBuilder T3] Attempting static scrape (Cheerio-style)...")
        try:
            from pipeline.t3_scrapers import StaticCheerioScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                StaticCheerioScraper.scrape_static_html(
                    CAREERBUILDER_JOBS_URL, "careerbuilder", CAREERBUILDER_SITEMAP_URL
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
                logger.info(f"[CareerBuilder T3] Static scrape returned {len(jobs)} jobs.")
                return jobs
        except Exception as exc:
            logger.info(f"[CareerBuilder T3] Static scrape failed ({exc}). Escalating to Playwright.")

        # Playwright fallback
        logger.info("[CareerBuilder T3] Attempting Playwright SPA fallback...")
        try:
            from pipeline.t3_scrapers import PlaywrightSpaScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                PlaywrightSpaScraper.scrape_playwright_spa(CAREERBUILDER_JOBS_URL, "careerbuilder")
            )
            loop.close()
            for j in jobs:
                if not j.get("job_type") or j["job_type"] == "unknown":
                    j["job_type"] = _parse_job_type_from_text(
                        f"{j.get('title','')} {j.get('description','')}"
                    )
                j.setdefault("source_tier", "Tier 3 (Playwright)")
            logger.info(f"[CareerBuilder T3] Playwright returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[CareerBuilder T3] Playwright error: {exc}")
        return []

    # ------------------------------------------------------------------
    # T4: SerpApi Google Jobs
    # ------------------------------------------------------------------
    def _fetch_t4_serpapi(self, keyword: str = "developer", country: str = "US", page: int = 1) -> List[Dict[str, Any]]:
        if not self.serp_key:
            logger.info("[CareerBuilder T4] No SERPAPI_API_KEY. Skipping T4.")
            return []
        logger.info(f"[CareerBuilder T4] SerpApi Google Jobs for query='{keyword}', page={page}")
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item, parse_relative_posted_date
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} careerbuilder",
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
                direct_link = extract_direct_url_from_serpapi_item(item, "careerbuilder")
                if not direct_link:
                    logger.debug(f"[CareerBuilder T4] Skipping job '{title}' â€” no direct CareerBuilder URL in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "CareerBuilder Employer")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": parse_relative_posted_date(ext.get("posted_at")) if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "platform_id": "careerbuilder",
                    "source_tier": "Tier 4 (SerpApi)",
                })
            logger.info(f"[CareerBuilder T4] SerpApi returned {len(jobs)} jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[CareerBuilder T4] SerpApi error: {exc}")
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
        """Orchestrates T1â†’T2â†’T3â†’T4. T5 cache handled by orchestrator.
        page > 1: only T4 supports real pagination."""
        import time
        if page > 1:
            return self._fetch_t4_serpapi(keyword, country, page=page)

        for attempt in range(1, 4):
            results = self._fetch_t1_developer_api(keyword, country)
            if results:
                return results
            if attempt < 3:
                time.sleep(2 ** attempt)

        for attempt in range(1, 4):
            results = self._fetch_t2_apify(keyword)
            if results:
                return results
            if attempt < 3:
                time.sleep(2 ** attempt)

        for attempt in range(1, 4):
            results = self._fetch_t3_cheerio_playwright(keyword)
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

        logger.warning("[CareerBuilder] All tiers T1â€“T4 returned 0 results. Triggering T5 via orchestrator.")
        return []



