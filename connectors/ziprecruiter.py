"""
ZipRecruiter Connector â€” 5-Tier Scraping Architecture
=====================================================
T1: Partner/Publisher API integration point (requires manual approval â€” falls through to T3 in the meantime)
T2: Apify Store actor build-time check
T3: Playwright SPA scraper + sitemap.xml discovery
T4: SerpApi Google Jobs scoped to site:ziprecruiter.com
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
ZIPRECRUITER_PARTNER_API_KEY = os.getenv("ZIPRECRUITER_PARTNER_API_KEY", "")

ZIPRECRUITER_JOBS_URL = "https://www.ziprecruiter.com/candidate/search"
ZIPRECRUITER_SITEMAP_URL = "https://www.ziprecruiter.com/sitemap.xml"


def _parse_job_type_from_text(text: str) -> str:
    """
    Parse job_type per-listing from posting content.
    ZipRecruiter mixes full-time, contract, and part-time â€” never hardcode.
    """
    lower = text.lower() if text else ""
    if any(w in lower for w in ["contract", "contractor", "freelance", "c2c", "corp-to-corp", "c-corp"]):
        return "contract"
    if any(w in lower for w in ["part-time", "part time", "parttime"]):
        return "part_time"
    if any(w in lower for w in ["full-time", "full time", "fulltime", "permanent"]):
        return "full_time"
    return "unknown"


class ZipRecruiterConnector:
    """
    ZipRecruiter connector implementing 5-tier scraping architecture.
    T1 (Partner API) is an integration point that requires manual approval â€”
    pipeline does not block waiting for it; falls through to T3 immediately.
    """

    def __init__(
        self,
        partner_key: Optional[str] = None,
        serp_key: Optional[str] = None,
        apify_token: Optional[str] = None,
    ):
        self.partner_key = partner_key or ZIPRECRUITER_PARTNER_API_KEY
        self.serp_key = serp_key or SERPAPI_KEY
        self.apify_token = apify_token or APIFY_TOKEN

    # ------------------------------------------------------------------
    # T1: Partner API integration point (manual approval required)
    # ------------------------------------------------------------------
    def _fetch_t1_partner_api(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """
        ZipRecruiter Partner API integration point.
        Requires manual application and approval â€” not instant self-serve.
        Logs warning and returns [] immediately if key not present, so pipeline
        falls through to T3 without blocking.
        """
        if not self.partner_key:
            logger.warning(
                "[ZipRecruiter T1] ZIPRECRUITER_PARTNER_API_KEY not configured. "
                "The ZipRecruiter Partner API requires manual application/approval at "
                "https://www.ziprecruiter.com/partner. Falling through to T3."
            )
            return []
        try:
            logger.info("[ZipRecruiter T1] Partner API key found â€” attempting search.")
            params = {
                "search": keyword,
                "api_key": self.partner_key,
                "jobs_per_page": 50,
            }
            with httpx.Client(timeout=10.0) as client:
                res = client.get("https://api.ziprecruiter.com/jobs/v1", params=params)
            if res.status_code != 200:
                logger.warning(f"[ZipRecruiter T1] Partner API HTTP {res.status_code}. Falling through.")
                return []
            data = res.json()
            jobs = []
            for item in data.get("jobs", []):
                title = item.get("name") or item.get("title")
                url = item.get("job_detail_url") or item.get("url")
                if not title or not url:
                    continue
                desc = item.get("snippet") or item.get("description") or ""
                jtype_raw = item.get("job_type") or item.get("employment_type") or ""
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("hiring_company", {}).get("name") or "ZipRecruiter Employer").strip(),
                    "url": str(url).strip(),
                    "location": str(item.get("location") or "Remote").strip(),
                    "remote": item.get("remote_position", False) or "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": item.get("posted_time") or item.get("date_posted"),
                    "description": str(desc).strip(),
                    "platform_id": "ziprecruiter",
                    "source_tier": "Tier 1 (Partner API)",
                })
            logger.info(f"[ZipRecruiter T1] Partner API returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[ZipRecruiter T1] Partner API error: {exc}. Falling through.")
            return []

    # ------------------------------------------------------------------
    # T2: Apify Store actor (build-time check)
    # ------------------------------------------------------------------
    def _fetch_t2_apify(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        if not self.apify_token:
            logger.info("[ZipRecruiter T2] No APIFY_API_TOKEN. Skipping T2.")
            return []
        try:
            from pipeline.apify_store import check_apify_actor_available
            import asyncio
            loop = asyncio.new_event_loop()
            actor_id = loop.run_until_complete(check_apify_actor_available("ziprecruiter"))
            loop.close()
            if not actor_id:
                logger.info("[ZipRecruiter T2] No ZipRecruiter actor in Apify Store. Skipping T2.")
                return []
            logger.info(f"[ZipRecruiter T2] Found actor '{actor_id}' â€” executing.")
            from apify_client import ApifyClient
            client = ApifyClient(token=self.apify_token)
            run = client.actor(actor_id).call(run_input={"keyword": keyword, "maxItems": 200})
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "defaultDatasetId", None)
            if status == "SUCCEEDED" and dataset_id:
                items = client.dataset(dataset_id).list_items().items
                return self._normalize_apify_items(items, actor_id)
        except Exception as exc:
            logger.warning(f"[ZipRecruiter T2] Apify error: {exc}")
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
                "company": str(item.get("company") or item.get("companyName") or "ZipRecruiter Employer").strip(),
                "url": str(url).strip(),
                "location": str(item.get("location") or "Remote").strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                "posted_date": item.get("postedAt") or item.get("postedDate"),
                "description": str(desc).strip(),
                "platform_id": "ziprecruiter",
                "source_tier": f"Tier 2 (Apify:{actor_id})",
            })
        return jobs

    # ------------------------------------------------------------------
    # T3: Playwright + sitemap.xml
    # ------------------------------------------------------------------
    def _fetch_t3_playwright(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        logger.info("[ZipRecruiter T3] Attempting Playwright SPA scrape...")
        try:
            from pipeline.t3_scrapers import PlaywrightSpaScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                PlaywrightSpaScraper.scrape_playwright_spa(ZIPRECRUITER_JOBS_URL, "ziprecruiter")
            )
            loop.close()
            for j in jobs:
                if not j.get("job_type") or j["job_type"] == "unknown":
                    j["job_type"] = _parse_job_type_from_text(
                        f"{j.get('title','')} {j.get('description','')}"
                    )
                j.setdefault("source_tier", "Tier 3 (Playwright)")
            logger.info(f"[ZipRecruiter T3] Playwright returned {len(jobs)} jobs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[ZipRecruiter T3] Playwright error: {exc}")
        return []

    # ------------------------------------------------------------------
    # T4: SerpApi Google Jobs scoped to ziprecruiter.com
    # ------------------------------------------------------------------
    def _fetch_t4_serpapi(self, keyword: str = "developer", country: str = "US", page: int = 1) -> List[Dict[str, Any]]:
        if not self.serp_key:
            logger.info("[ZipRecruiter T4] No SERPAPI_API_KEY. Skipping T4.")
            return []
        logger.info(f"[ZipRecruiter T4] SerpApi Google Jobs for query='{keyword}', page={page}")
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} ziprecruiter",
                "api_key": self.serp_key,
            }
            if page > 1:
                params["start"] = str((page - 1) * 10)
            with httpx.Client(timeout=15.0) as client:
                res = client.get("https://serpapi.com/search.json", params=params)
            if res.status_code != 200:
                logger.warning(f"[ZipRecruiter T4] SerpApi HTTP {res.status_code}")
                return []
            jobs = []
            for item in res.json().get("jobs_results", []):
                title = item.get("title")
                direct_link = extract_direct_url_from_serpapi_item(item, "ziprecruiter")
                if not direct_link:
                    logger.debug(f"[ZipRecruiter T4] Skipping job '{title}' â€” no direct ZipRecruiter URL in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "ZipRecruiter Employer")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "platform_id": "ziprecruiter",
                    "source_tier": "Tier 4 (SerpApi)",
                })
            logger.info(f"[ZipRecruiter T4] SerpApi returned {len(jobs)} jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[ZipRecruiter T4] SerpApi error: {exc}")
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
        """
        Orchestrates T1â†’T2â†’T3â†’T4 (T1 is a no-op until partner key is provisioned).
        T5 cache fallback is handled by the FiveTierScraperOrchestrator.
        page > 1: only T4 supports real pagination.
        """
        import time
        if page > 1:
            return self._fetch_t4_serpapi(keyword, country, page=page)

        for attempt in range(1, 4):
            results = self._fetch_t1_partner_api(keyword)
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
            results = self._fetch_t3_playwright(keyword)
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

        logger.warning("[ZipRecruiter] All tiers T1â€“T4 returned 0 results. Triggering T5 via orchestrator.")
        return []



