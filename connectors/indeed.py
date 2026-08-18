import os
import time
import logging
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class IndeedAPIError(Exception):
    """Custom exception for Indeed API/Scraper errors."""
    pass


class IndeedConnector:
    """
    Indeed Connector with 2-Layer Architecture:
      1. Primary: SerpApi Google Jobs engine (q="{keyword} indeed")
      2. Fallback: Apify Indeed actor chain
    """

    ACTOR_CHAIN = [
        "misceres/indeed-scraper",
        "borderline/indeed-scraper",
        "kaix/indeed-scraper",
    ]

    def __init__(self, token: Optional[str] = None, serp_key: Optional[str] = None, timeout: float = 90.0, max_retries: int = 3):
        self.token = token or os.getenv("APIFY_API_TOKEN")
        self.serp_key = serp_key or os.getenv("SERPAPI_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_items = int(os.getenv("INDEED_MAX_ITEMS", "200"))
        self.client = None
        if self.token:
            try:
                from apify_client import ApifyClient
                self.client = ApifyClient(token=self.token)
            except ImportError:
                logger.warning("[Indeed] apify-client package not installed.")

    def fetch_jobs(
        self,
        keyword: str = "developer",
        country: str = "us",
        page: int = 1,
        where: Optional[str] = None,
        since_hours: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetches job search results from Indeed via Primary (SerpApi) -> Fallback (Apify).

        `since_hours` scopes the query to recent postings at the SOURCE rather
        than fetching a default result set and filtering afterwards. Indeed's
        native lever is `fromage` (max age in DAYS) plus `sort=date`; the Apify
        actors accept both. Sub-day windows clamp to fromage=1, which is the
        finest granularity Indeed exposes -- our own freshness filter then
        narrows further.
        """
        _fromage = None
        if since_hours:
            _fromage = max(1, int(round(since_hours / 24.0)))
        search_query = keyword.strip() if keyword else "developer"
        location_name = "United States" if country.upper() == "US" else ("India" if country.upper() == "IN" else country)
        if where:
            location_name = where.strip()

        # LAYER 1: Primary SerpApi Google Jobs
        if self.serp_key:
            try:
                import httpx
                from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
                logger.info(f"[Indeed] Trying Primary (SerpApi Google Jobs) for query='{search_query}'...")
                params = {
                    "engine": "google_jobs",
                    "q": f"{search_query} indeed",
                    "api_key": self.serp_key,
                }
                if since_hours:
                    params["chips"] = (
                        "date_posted:today" if since_hours <= 24
                        else "date_posted:3days" if since_hours <= 72
                        else "date_posted:week" if since_hours <= 168
                        else "date_posted:month"
                    )
                if location_name:
                    params["location"] = location_name
                if page > 1:
                    params["start"] = str((page - 1) * 10)
                res = httpx.get("https://serpapi.com/search.json", params=params, timeout=15.0)
                if res.status_code == 200:
                    raw_jobs = res.json().get("jobs_results", [])
                    results = []
                    for item in raw_jobs:
                        title = item.get("title")
                        direct_link = extract_direct_url_from_serpapi_item(item, "indeed")
                        if not direct_link:
                            continue
                        if title:
                            results.append({
                                "title": str(title).strip(),
                                "company": str(item.get("company_name", "Indeed Employer")).strip(),
                                "url": str(direct_link).strip(),
                                "location": str(item.get("location", location_name)).strip(),
                                "remote": "remote" in str(item.get("location", "")).lower(),
                                "contract_type": "full_time",
                                "posted_date": item.get("detected_extensions", {}).get("posted_at") if isinstance(item.get("detected_extensions"), dict) else None,
                                "description": str(item.get("description", "")).strip(),
                                "source_actor": "serpapi_google_jobs_indeed",
                            })
                    if results:
                        logger.info(f"[Indeed] SUCCESS: SerpApi returned {len(results)} live Indeed jobs.")
                        return results
            except Exception as e:
                logger.warning(f"[Indeed] SerpApi fetch failed: {e}")

        for actor_idx, actor_id in enumerate(self.ACTOR_CHAIN, 1):
            logger.info(
                f"[Indeed] Trying Actor #{actor_idx} ('{actor_id}') with maxItems={self.max_items} "
                f"for query='{search_query}', location='{location_name}'..."
            )

            run_input = {
                "position": search_query,
                "query": search_query,
                "search": search_query,
                "searchKeyword": search_query,
                "country": country.upper(),
                "location": location_name,
                "maxItems": self.max_items,
                "limit": self.max_items,
            }
            if _fromage is not None:
                # Native source-side recency. Harmless on actors that ignore
                # unknown keys, and a large win on the ones that honour it.
                run_input["fromage"] = _fromage
                run_input["maxDaysOld"] = _fromage
                run_input["sort"] = "date"
                logger.info(
                    f"[Indeed] Scoping to fromage={_fromage}d, sort=date "
                    f"(since_hours={since_hours})."
                )

            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(f"[Indeed] Invoking '{actor_id}' (Attempt {attempt}/{self.max_retries})...")
                    run = self.client.actor(actor_id).call(run_input=run_input)

                    status = run.get("status") if isinstance(run, dict) else getattr(run, "status", "UNKNOWN")
                    dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else (getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None))

                    if status == "SUCCEEDED" and dataset_id:
                        items = self.client.dataset(dataset_id).list_items().items
                        raw_jobs = self._parse_indeed_items(items, actor_id)

                        stats = run.get("stats", {}) if isinstance(run, dict) else getattr(run, "stats", {})
                        compute_units = stats.get("computeUnits", 0) if isinstance(stats, dict) else getattr(stats, "compute_units", 0)
                        est_cost_usd = round(float(compute_units or 0) * 0.25, 4)

                        if raw_jobs:
                            logger.info(
                                f"[Indeed] SUCCESS: Actor #{actor_idx} ('{actor_id}') returned {len(raw_jobs)} real Indeed jobs. "
                                f"Apify compute units: {compute_units} (~${est_cost_usd} USD)."
                            )
                            return raw_jobs
                        else:
                            logger.info(f"[Indeed] Actor #{actor_idx} ('{actor_id}') returned 0 valid items. Falling to next actor.")
                            break
                    else:
                        logger.warning(f"[Indeed] Actor '{actor_id}' finished with status '{status}' (dataset_id='{dataset_id}').")

                except Exception as exc:
                    err_msg = str(exc)
                    if "usage hard limit" in err_msg.lower() or "quota" in err_msg.lower() or "rate" in err_msg.lower():
                        logger.warning(f"[Indeed] Apify quota/limit hit: {exc}. Fast-failing Apify fallback.")
                        break
                    wait_time = 2 ** attempt
                    logger.warning(f"[Indeed] Actor '{actor_id}' attempt {attempt} failed: {exc}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)

        logger.warning("[Indeed] All actors in fallback chain returned 0 results or failed. Returning empty list.")
        return []

    def _parse_indeed_items(self, items: List[Any], actor_id: str) -> List[Dict[str, Any]]:
        raw_jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue

            title = item.get("title") or item.get("positionName") or item.get("position") or item.get("jobTitle")
            apply_url = item.get("url") or item.get("externalApplyLink") or item.get("jobUrl") or item.get("link") or item.get("externalApplyUrl")
            company = item.get("company") or item.get("companyName") or item.get("employer") or "Indeed Employer"

            if isinstance(company, dict):
                company = company.get("name") or company.get("display_name") or "Indeed Employer"

            if not title or not apply_url:
                continue

            loc_data = item.get("location") or item.get("displayLocation") or item.get("city") or "US"
            desc = item.get("description") or item.get("snippet") or item.get("summary") or ""

            raw_jobs.append({
                "title": str(title).strip(),
                "company": str(company).strip(),
                "url": str(apply_url).strip(),
                "location": str(loc_data).strip(),
                "remote": "remote" in str(loc_data).lower() or "wfh" in str(loc_data).lower() or "work from home" in str(title).lower(),
                "contract_type": str(item.get("jobType") or item.get("contractType") or "full_time"),
                "posted_date": item.get("postedAt") or item.get("postedDate") or item.get("created"),
                "description": str(desc).strip(),
                "source_actor": actor_id,
            })
        return raw_jobs

