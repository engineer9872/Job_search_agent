import os
import time
import logging
import httpx
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class LinkedInJobsAPIError(Exception):
    """Custom exception for LinkedIn Jobs API errors."""
    pass


class LinkedInJobsConnector:
    """
    LinkedIn Jobs Connector with 2-Layer Fallback Chain:
      1. Primary: SerpApi Google Jobs engine (q="{keyword} site:linkedin.com/jobs")
      2. Fallback: Apify LinkedIn Jobs actor (e.g. curious_coder/linkedin-jobs-scraper or apify/linkedin-jobs-scraper)
    Never blocks the pipeline — returns empty list [] and logs warning if all methods fail. Zero mock/fake data.
    """

    APIFY_ACTORS = [
        "curious_coder/linkedin-jobs-scraper",
        "apify/linkedin-jobs-scraper",
    ]

    def __init__(self, token: Optional[str] = None, serp_key: Optional[str] = None):
        self.token = token or os.getenv("APIFY_API_TOKEN")
        self.serp_key = serp_key or os.getenv("SERPAPI_API_KEY")

    def fetch_linkedin_jobs(
        self, keyword: str = "developer", country: str = "IN", page: int = 1,
        where: Optional[str] = None, since_hours: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches live LinkedIn job listings using Primary (SerpApi) -> Fallback (Apify).
        """
        search_keyword = keyword.strip() if keyword else "developer"
        location_name = where if where else ("India" if country.upper() == "IN" else ("United States" if country.upper() == "US" else country.upper()))

        # LAYER 1: SerpApi Google Jobs search (site:linkedin.com/jobs)
        if self.serp_key:
            try:
                from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
                logger.info(f"[LinkedIn] Trying Primary (SerpApi Google Jobs) for query='{search_keyword}'...")
                url = "https://serpapi.com/search.json"

                params = {
                    "engine": "google_jobs",
                    "q": f"{search_keyword} linkedin",
                    "location": location_name,
                    "api_key": self.serp_key,
                }
                if page > 1:
                    params["start"] = str((page - 1) * 10)

                with httpx.Client(timeout=15.0) as client:
                    res = client.get(url, params=params)

                if res.status_code == 200:
                    data = res.json()
                    raw_jobs = data.get("jobs_results", [])
                    results = []
                    for item in raw_jobs:
                        title = item.get("title")
                        company = item.get("company_name", "LinkedIn Employer")
                        # Extract direct LinkedIn job-posting URL from apply_options
                        direct_link = extract_direct_url_from_serpapi_item(item, "linkedin")
                        if not direct_link:
                            logger.debug(f"[LinkedIn] Skipping job '{title}' — no direct LinkedIn URL found in apply_options")
                            continue
                        if title:
                            results.append({
                                "title": str(title).strip(),
                                "company": str(company).strip(),
                                "url": str(direct_link).strip(),
                                "location": item.get("location") or f"Remote / {country}",
                                "remote": "remote" in str(item.get("location", "")).lower(),
                                "contract_type": "full_time",
                                "posted_date": item.get("detected_extensions", {}).get("posted_at") if isinstance(item.get("detected_extensions"), dict) else None,
                                "description": str(item.get("description", "")).strip(),
                                "source_actor": "serpapi_google_jobs_linkedin",
                            })
                    if results:
                        logger.info(f"[LinkedIn] SUCCESS: SerpApi returned {len(results)} live LinkedIn jobs with direct URLs.")
                        return results
                    else:
                        logger.info("[LinkedIn] SerpApi query returned 0 jobs. Falling to Apify LinkedIn actor...")
                else:
                    logger.warning(f"[LinkedIn] SerpApi returned HTTP {res.status_code}: {res.text[:200]}")
            except Exception as e:
                logger.warning(f"[LinkedIn] SerpApi LinkedIn fetch error: {e}")

        # LAYER 2: Apify LinkedIn Jobs Scraper Fallback
        if self.token:
            try:
                from apify_client import ApifyClient
                client = ApifyClient(token=self.token)

                for actor_id in self.APIFY_ACTORS:
                    logger.info(f"[LinkedIn] Trying Apify Actor fallback '{actor_id}'...")
                    run_input = {
                        "searchKeyword": search_keyword,
                        "keyword": search_keyword,
                        "location": location_name,
                        "maxItems": 300,
                        "limit": 300,
                    }
                    if since_hours:
                        # LinkedIn's native "date posted" lever is f_TPR=r<seconds>.
                        # Most LinkedIn actors accept it either as a raw param or
                        # as datePosted/publishedAt. Unknown keys are ignored by
                        # the actors that don't support it.
                        run_input["f_TPR"] = f"r{int(since_hours) * 3600}"
                        run_input["datePosted"] = (
                            "past24Hours" if since_hours <= 24
                            else "pastWeek" if since_hours <= 168 else "pastMonth"
                        )
                        run_input["sortBy"] = "DD"  # date descending
                        logger.info(
                            f"[LinkedIn] Scoping to f_TPR=r{int(since_hours)*3600} "
                            f"(since_hours={since_hours}), sortBy=date."
                        )
                    for attempt in range(1, 4):
                        try:
                            run = client.actor(actor_id).call(run_input=run_input)
                            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
                            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else (getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None))

                            if status == "SUCCEEDED" and dataset_id:
                                items = client.dataset(dataset_id).list_items().items
                                raw_jobs = []
                                for item in items:
                                    if isinstance(item, dict):
                                        title = item.get("title") or item.get("positionName") or item.get("jobTitle") or item.get("role")
                                        # curious_coder uses "link" as the canonical job URL; applyUrl can be empty
                                        link = (
                                            item.get("link") or item.get("url")
                                            or item.get("jobUrl") or item.get("jobURL")
                                            or (item.get("applyUrl") if item.get("applyUrl") else None)
                                        )
                                        if title and link:
                                            raw_jobs.append({
                                                "title": str(title).strip(),
                                                "company": str(item.get("companyName") or item.get("company") or "LinkedIn Employer").strip(),
                                                "url": str(link).strip(),
                                                "location": str(item.get("location") or country).strip(),
                                                "remote": "remote" in str(item.get("location", "")).lower(),
                                                "contract_type": str(item.get("employmentType") or item.get("contractType") or "Full-time"),
                                                "posted_date": item.get("postedAt") or item.get("postedDate"),
                                                "description": str(item.get("descriptionText") or item.get("description") or "").strip(),
                                                "source_actor": actor_id,
                                            })
                                if raw_jobs:
                                    logger.info(f"[LinkedIn] SUCCESS: Apify actor '{actor_id}' returned {len(raw_jobs)} live LinkedIn jobs.")
                                    return raw_jobs
                                else:
                                    break
                        except Exception as exc:
                            err_msg = str(exc)
                            if "usage hard limit" in err_msg.lower() or "quota" in err_msg.lower() or "rate" in err_msg.lower():
                                logger.warning(f"[LinkedIn] Apify quota/limit hit: {exc}. Fast-failing Apify fallback.")
                                break
                            wait_time = 2 ** attempt
                            logger.warning(f"[LinkedIn] Apify actor '{actor_id}' attempt {attempt} failed: {exc}. Retrying in {wait_time}s...")
                            time.sleep(wait_time)
            except Exception as exc:
                logger.warning(f"[LinkedIn] Apify LinkedIn fallback failed: {exc}")

        logger.warning("[LinkedIn] Both SerpApi and Apify LinkedIn scrapers returned zero results or failed. Returning empty list [] (no mock data inserted).")
        return []

    def fetch_jobs(self, keyword: str = "developer", country: str = "IN", page: int = 1,
                   where: Optional[str] = None, since_hours: Optional[int] = None) -> List[Dict[str, Any]]:
        """Alias for fetch_linkedin_jobs to align with standard connector interface."""
        return self.fetch_linkedin_jobs(keyword=keyword, country=country, page=page,
                                        where=where, since_hours=since_hours)
