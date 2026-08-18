import os
import time
import logging
import httpx
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class GlassdoorAPIError(Exception):
    """Custom exception for Glassdoor API/Scraper errors."""
    pass


class GlassdoorConnector:
    """
    Glassdoor Connector with 2-Layer Fallback Chain:
      1. Primary: SerpApi Google Jobs engine (q="{keyword} site:glassdoor.com/job-listing")
      2. Fallback: Apify Glassdoor actor (e.g. blakep/glassdoor-scraper or apify/glassdoor-scraper)
    Never blocks the pipeline — returns empty list [] and logs warning if all methods fail. Zero mock/fake data.
    """

    APIFY_ACTORS = [
        "blakep/glassdoor-scraper",
        "apify/glassdoor-scraper",
    ]

    def __init__(self, token: Optional[str] = None, serp_key: Optional[str] = None):
        self.token = token or os.getenv("APIFY_API_TOKEN")
        self.serp_key = serp_key or os.getenv("SERPAPI_API_KEY")

    def fetch_jobs(
        self, keyword: str = "developer", country: str = "us", page: int = 1, where: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches live Glassdoor job listings using Primary (SerpApi) -> Fallback (Apify).
        """
        search_keyword = keyword.strip() if keyword else "developer"

        # LAYER 1: SerpApi Google Jobs search (site:glassdoor.com/job-listing)
        if self.serp_key:
            try:
                from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
                logger.info(f"[Glassdoor] Trying Primary (SerpApi Google Jobs) for query='{search_keyword}'...")
                url = "https://serpapi.com/search.json"
                location_name = "United States" if country.upper() == "US" else ("India" if country.upper() == "IN" else country)
                if where:
                    location_name = where

                params = {
                    "engine": "google_jobs",
                    "q": f"{search_keyword} glassdoor",
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
                        company = item.get("company_name", "Glassdoor Employer")
                        direct_link = extract_direct_url_from_serpapi_item(item, "glassdoor")
                        if not direct_link:
                            logger.debug(f"[Glassdoor] Skipping job '{title}' — no direct Glassdoor URL found in apply_options")
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
                                "source_actor": "serpapi_google_jobs_glassdoor",
                            })
                    if results:
                        logger.info(f"[Glassdoor] SUCCESS: SerpApi returned {len(results)} live Glassdoor jobs with direct URLs.")
                        return results
                    else:
                        logger.info("[Glassdoor] SerpApi query returned 0 jobs. Falling to Apify Glassdoor actor...")
                else:
                    logger.warning(f"[Glassdoor] SerpApi returned HTTP {res.status_code}: {res.text[:200]}")
            except Exception as e:
                logger.warning(f"[Glassdoor] SerpApi Glassdoor fetch error: {e}")

        # LAYER 2: Apify Glassdoor Scraper Fallback
        if self.token:
            try:
                from apify_client import ApifyClient
                client = ApifyClient(token=self.token)

                for actor_id in self.APIFY_ACTORS:
                    logger.info(f"[Glassdoor] Trying Apify Actor fallback '{actor_id}'...")
                    run_input = {
                        "query": search_keyword,
                        "keyword": search_keyword,
                        "location": country.upper(),
                        "maxItems": 200,
                        "limit": 30,
                    }
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
                                        title = item.get("title") or item.get("jobTitle") or item.get("positionName") or item.get("position")
                                        link = item.get("url") or item.get("link") or item.get("jobUrl") or item.get("applyUrl")
                                        if title and link:
                                            raw_jobs.append({
                                                "title": str(title).strip(),
                                                "company": str(item.get("companyName") or item.get("company") or "Glassdoor Employer").strip(),
                                                "url": str(link).strip(),
                                                "location": str(item.get("location") or country).strip(),
                                                "remote": "remote" in str(item.get("location", "")).lower(),
                                                "contract_type": str(item.get("contractType") or "full_time"),
                                                "posted_date": item.get("postedAt") or item.get("postedDate"),
                                                "description": str(item.get("description") or "").strip(),
                                                "source_actor": actor_id,
                                            })
                                if raw_jobs:
                                    logger.info(f"[Glassdoor] SUCCESS: Apify actor '{actor_id}' returned {len(raw_jobs)} live Glassdoor jobs.")
                                    return raw_jobs
                                else:
                                    break
                        except Exception as exc:
                            err_msg = str(exc)
                            if "usage hard limit" in err_msg.lower() or "quota" in err_msg.lower() or "rate" in err_msg.lower():
                                logger.warning(f"[Glassdoor] Apify quota/limit hit: {exc}. Fast-failing Apify fallback.")
                                break
                            wait_time = 2 ** attempt
                            logger.warning(f"[Glassdoor] Apify actor '{actor_id}' attempt {attempt} failed: {exc}. Retrying in {wait_time}s...")
                            time.sleep(wait_time)
            except Exception as exc:
                logger.warning(f"[Glassdoor] Apify Glassdoor fallback failed: {exc}")

        logger.warning("[Glassdoor] Both SerpApi and Apify Glassdoor scrapers returned zero results or failed. Returning empty list [] (no mock data inserted).")
        return []
