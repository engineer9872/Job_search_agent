import os
import time
import logging
import httpx
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class GoogleJobsAPIError(Exception):
    """Custom exception for SerpApi Google Jobs API errors."""
    pass


class GoogleJobsConnector:
    """
    Connector for fetching job listings from Google Jobs via SerpApi (Free Tier).
    Docs: https://serpapi.com/google-jobs-api
    Source Platform: 'google_jobs'
    """

    BASE_URL = "https://serpapi.com/search.json"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries

    def fetch_jobs(
        self,
        keyword: str,
        country: str = "us",
        page: int = 1,
        where: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetches live Google Jobs listings via SerpApi.

        Args:
            keyword: Job title / keyword (e.g. 'python developer')
            country: 2-letter country code (US, IN, GB, CA, AU, DE)
            page: Page offset number
            where: Optional city/location string

        Returns:
            List of raw job dictionaries returned by SerpApi Google Jobs engine.
        """
        if not self.api_key:
            logger.warning("[GoogleJobs] SERPAPI_API_KEY is not set in environment. Returning empty list.")
            return []

        country_name_map = {
            "us": "United States",
            "in": "India",
            "gb": "United Kingdom",
            "ca": "Canada",
            "au": "Australia",
            "de": "Germany",
        }

        location = where if where else country_name_map.get(country.lower(), "United States")
        query_str = f"{keyword} in {location}" if keyword else f"developer in {location}"

        params = {
            "engine": "google_jobs",
            "q": query_str,
            "location": location,
            "api_key": self.api_key,
        }
        if page > 1:
            params["start"] = str((page - 1) * 10)

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    f"[GoogleJobs] Calling SerpApi Google Jobs engine ({attempt}/{self.max_retries}) - query: '{query_str}', location: '{location}'"
                )
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(self.BASE_URL, params=params)

                if response.status_code == 200:
                    payload = response.json()
                    jobs = payload.get("jobs_results", [])
                    logger.info(f"[GoogleJobs] SerpApi call succeeded - returned {len(jobs)} Google Jobs listings.")
                    return jobs

                elif response.status_code in (401, 403):
                    logger.error(
                        f"[GoogleJobs] SerpApi Key Authentication Error (HTTP {response.status_code}): {response.text}. "
                        f"Check SERPAPI_API_KEY in .env."
                    )
                    return []

                elif response.status_code == 429:
                    wait_time = 2 ** attempt
                    logger.warning(f"[GoogleJobs] SerpApi rate limit (HTTP 429). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"[GoogleJobs] SerpApi HTTP Error {response.status_code}: {response.text}")
                    if attempt == self.max_retries:
                        return []

            except httpx.RequestError as exc:
                logger.error(f"[GoogleJobs] Network error while calling SerpApi: {exc}")
                if attempt == self.max_retries:
                    return []

        return []
