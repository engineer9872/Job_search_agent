"""
Hired Connector â€” 5-Tier Scraping Architecture
==============================================
T1: No public API â€” candidate-matching platform, not an open listing board
T2: Apify Store actor build-time check (low likelihood of match)
T3: Playwright narrowly scoped to whatever public pages exist (low yield expected)
T4: SerpApi Google Jobs scoped to site:hired.com (low expected yield)
T5: Cache fallback â€” PRIMARY path for this platform (handled by orchestrator)

Data characteristics:
  - data_completeness: "limited" tag on all results (per spec)
  - low_yield_platform: true â€” exclude from standard T5 failure-alert threshold
  - Low yield is expected platform behavior, NOT a scraper failure

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

HIRED_JOBS_URL = "https://hired.com/jobs"


def _parse_job_type_from_text(text: str) -> str:
    """
    Parse job_type per-listing from posting content.
    Hired mixes full-time and contract â€” never hardcode.
    """
    lower = text.lower() if text else ""
    if any(w in lower for w in ["contract", "contractor", "freelance", "c2c", "corp-to-corp"]):
        return "contract"
    if any(w in lower for w in ["part-time", "part time", "parttime"]):
        return "part_time"
    if any(w in lower for w in ["full-time", "full time", "fulltime", "permanent"]):
        return "full_time"
    return "unknown"


def _tag_limited(job: Dict[str, Any]) -> Dict[str, Any]:
    """Apply data_completeness: 'limited' tag to all Hired results per spec."""
    job["data_completeness"] = "limited"
    job["platform_id"] = "hired"
    return job


class HiredConnector:
    """
    Hired connector implementing 5-tier scraping architecture.

    IMPORTANT: Hired is a candidate-matching platform, NOT an open job board.
    Low yield is expected platform behavior â€” do not treat it as a scraper failure.
    T5 cache fallback is the primary result source for this platform.

    All results are tagged data_completeness='limited' per spec.
    This platform is excluded from the standard Tier 5 failure-alert threshold
    (low_yield_platform: true in portals_config.json).
    """

    def __init__(
        self,
        serp_key: Optional[str] = None,
        apify_token: Optional[str] = None,
    ):
        self.serp_key = serp_key or SERPAPI_KEY
        self.apify_token = apify_token or APIFY_TOKEN

    # ------------------------------------------------------------------
    # T1: No public API â€” unavailable
    # ------------------------------------------------------------------
    def _fetch_t1(self) -> List[Dict[str, Any]]:
        """T1 is unavailable for Hired â€” no public API (candidate-matching platform)."""
        logger.info(
            "[Hired T1] No public API â€” Hired is a candidate-matching platform, "
            "not an open listing board. T1 unavailable."
        )
        return []

    # ------------------------------------------------------------------
    # T2: Apify Store actor (build-time check â€” low likelihood)
    # ------------------------------------------------------------------
    def _fetch_t2_apify(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """
        Check Apify Store for a Hired actor. Low likelihood of match given
        platform structure â€” checking anyway per spec.
        """
        if not self.apify_token:
            logger.info("[Hired T2] No APIFY_API_TOKEN. Skipping T2.")
            return []
        try:
            from pipeline.apify_store import check_apify_actor_available
            import asyncio
            loop = asyncio.new_event_loop()
            actor_id = loop.run_until_complete(check_apify_actor_available("hired"))
            loop.close()
            if not actor_id:
                logger.info("[Hired T2] No Hired actor in Apify Store (expected â€” platform structure). Skipping T2.")
                return []
            logger.info(f"[Hired T2] Found actor '{actor_id}' â€” executing.")
            from apify_client import ApifyClient
            client = ApifyClient(token=self.apify_token)
            run = client.actor(actor_id).call(run_input={"keyword": keyword, "maxItems": 200})
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "defaultDatasetId", None)
            if status == "SUCCEEDED" and dataset_id:
                items = client.dataset(dataset_id).list_items().items
                return [_tag_limited(j) for j in self._normalize_apify_items(items, actor_id)]
        except Exception as exc:
            logger.warning(f"[Hired T2] Apify error: {exc}")
        return []

    def _normalize_apify_items(self, items, actor_id: str) -> List[Dict[str, Any]]:
        jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("jobTitle") or item.get("positionName") or item.get("role")
            url = item.get("url") or item.get("link") or item.get("jobUrl") or item.get("applyUrl")
            if not title or not url:
                continue
            desc = item.get("description") or item.get("summary") or ""
            jtype_raw = item.get("employmentType") or item.get("jobType") or item.get("contractType") or ""
            jobs.append({
                "title": str(title).strip(),
                "company": str(item.get("company") or item.get("companyName") or "Hired Employer").strip(),
                "url": str(url).strip(),
                "location": str(item.get("location") or "Remote").strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                "posted_date": item.get("postedAt") or item.get("postedDate"),
                "description": str(desc).strip(),
                "source_tier": f"Tier 2 (Apify:{actor_id})",
            })
        return jobs

    # ------------------------------------------------------------------
    # T3: Playwright narrowly scoped to public pages
    # ------------------------------------------------------------------
    def _fetch_t3_playwright(self, keyword: str = "developer") -> List[Dict[str, Any]]:
        """
        Playwright scraper narrowly scoped to whatever public job/company pages exist.
        Do not over-engineer this. Low yield is expected platform behavior.
        """
        logger.info("[Hired T3] Attempting Playwright scrape (narrowly scoped â€” low yield expected)...")
        try:
            from pipeline.t3_scrapers import PlaywrightSpaScraper
            import asyncio
            loop = asyncio.new_event_loop()
            jobs = loop.run_until_complete(
                PlaywrightSpaScraper.scrape_playwright_spa(HIRED_JOBS_URL, "hired")
            )
            loop.close()
            result = []
            for j in jobs:
                if not j.get("job_type") or j["job_type"] == "unknown":
                    j["job_type"] = _parse_job_type_from_text(
                        f"{j.get('title','')} {j.get('description','')}"
                    )
                j.setdefault("source_tier", "Tier 3 (Playwright)")
                result.append(_tag_limited(j))
            if result:
                logger.info(f"[Hired T3] Playwright returned {len(result)} jobs.")
            else:
                logger.info("[Hired T3] Playwright returned 0 jobs (expected â€” platform is candidate-matching).")
            return result
        except Exception as exc:
            logger.warning(f"[Hired T3] Playwright error: {exc}")
        return []

    # ------------------------------------------------------------------
    # T4: SerpApi Google Jobs (low expected yield)
    # ------------------------------------------------------------------
    def _fetch_t4_serpapi(self, keyword: str = "developer", country: str = "US", page: int = 1) -> List[Dict[str, Any]]:
        """Low expected yield from Google Jobs for Hired â€” attempt anyway as low-cost."""
        if not self.serp_key:
            logger.info("[Hired T4] No SERPAPI_API_KEY. Skipping T4.")
            return []
        logger.info(f"[Hired T4] SerpApi Google Jobs for query='{keyword}', page={page} (low yield expected).")
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} hired",
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
                direct_link = extract_direct_url_from_serpapi_item(item, "hired")
                if not direct_link:
                    logger.debug(f"[Hired T4] Skipping job '{title}' â€” no direct Hired URL in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                job = {
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "Hired Employer")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "Remote")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": _parse_job_type_from_text(f"{jtype_raw} {desc}"),
                    "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "source_tier": "Tier 4 (SerpApi)",
                }
                jobs.append(_tag_limited(job))
            logger.info(f"[Hired T4] SerpApi returned {len(jobs)} Hired jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[Hired T4] SerpApi error: {exc}")
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
        Orchestrates T1(skip)â†’T2â†’T3â†’T4.
        Low yield across all tiers is expected â€” T5 cache is the primary result
        path for this platform. T5 fallback handled by the orchestrator.
        This platform is excluded from the standard T5 failure-alert threshold.
        page > 1: only T4 supports real pagination.
        """
        if page > 1:
            return self._fetch_t4_serpapi(keyword, country, page=page)

        # T1 always returns [] (no public API)
        self._fetch_t1()

        results = self._fetch_t2_apify(keyword)
        if results:
            return results

        results = self._fetch_t3_playwright(keyword)
        if results:
            return results

        results = self._fetch_t4_serpapi(keyword, country, page=1)
        if results:
            return results

        logger.info(
            "[Hired] All tiers T2â€“T4 returned 0 results (expected for this platform). "
            "T5 cache fallback will be used by the orchestrator."
        )
        return []



