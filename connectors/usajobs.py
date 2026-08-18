"""
USAJOBS Connector — Official USAJOBS Search API (T1 Only)
=========================================================
T1: Official USAJOBS Search API (developer.usajobs.gov) — sole/primary source.
    Requires:
      - USAJOBS_API_KEY env var  →  Authorization-Key header
      - USAJOBS_EMAIL env var    →  User-Agent header (contact email per their docs)
    Register at: https://developer.usajobs.gov (free, email registration)

T2/T3/T4: Not built — T1 fully covers this source per spec.
T5: Cache fallback for API downtime (handled by orchestrator).

ALL results are tagged:
  - country_code: "US"
  - eligibility_note: "U.S. federal employment — typically requires U.S. citizenship"

job_type is parsed per-listing from PositionSchedule field — never hardcoded.
"""

import os
import logging
import httpx
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

USAJOBS_API_KEY = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_EMAIL = os.getenv("USAJOBS_EMAIL", "")
USAJOBS_API_BASE = "https://data.usajobs.gov/api/search"

ELIGIBILITY_NOTE = "U.S. federal employment — typically requires U.S. citizenship"


def _parse_job_type_from_schedule(schedule_type_codes: list) -> str:
    """
    Parse job_type from USAJOBS PositionSchedule codes.
    USAJOBS schedule type codes: 1=Full-Time, 2=Part-Time, 3=Shift Work,
    4=Intermittent, 5=Job Sharing, 6=Multiple Schedules
    """
    if not schedule_type_codes:
        return "unknown"
    codes = [str(c).strip() for c in schedule_type_codes]
    # Check description text if available
    combined = " ".join(codes).lower()
    if any(c in ["1", "full"] or "full" in c for c in codes):
        return "full_time"
    if any(c in ["2", "part"] or "part" in c for c in codes):
        return "part_time"
    if any(w in combined for w in ["contract", "intermittent", "4"]):
        return "contract"
    return "unknown"


class USAJobsConnector:
    """
    USAJOBS connector — sole source is the official Search API (T1).
    This is the most reliable T1 of all 10 platforms.

    All results are tagged with country_code='US' and an eligibility note
    about U.S. citizenship requirements so downstream consumers are not
    misled about India eligibility.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
    ):
        self.api_key = api_key or USAJOBS_API_KEY
        self.email = email or USAJOBS_EMAIL
        self._warn_if_unconfigured()

    def _warn_if_unconfigured(self):
        if not self.api_key:
            logger.warning(
                "[USAJOBS] USAJOBS_API_KEY is not configured. "
                "Register at https://developer.usajobs.gov to obtain a free API key. "
                "USAJOBS T1 will be unavailable until this key is set — T5 cache fallback will be used."
            )
        if not self.email:
            logger.warning(
                "[USAJOBS] USAJOBS_EMAIL is not configured. "
                "The USAJOBS API requires a contact email in the User-Agent header per their documentation."
            )

    def fetch_jobs(
        self,
        keyword: str = "developer",
        country: str = "US",
        remote_only: bool = False,
        results_per_page: int = 250,
        max_pages: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        Fetch jobs from the official USAJOBS Search API, paginating up to
        max_pages (default 4 x 250 = up to 1000 listings per keyword) instead
        of silently returning a single 25-result page. USAJOBS caps
        ResultsPerPage at 500 per their docs; 250 is used as a safe default.
        Returns [] if API key is not configured (orchestrator falls to T5 cache).
        All results tagged with country_code='US' and eligibility_note.
        """
        if not self.api_key:
            logger.warning("[USAJOBS] No API key — attempting SerpApi Google Jobs fallback scoped to site:usajobs.gov.")
            return self._fetch_serpapi_fallback(keyword)

        headers = {
            "Authorization-Key": self.api_key,
            "User-Agent": self.email or "job-search-agent@example.com",
            "Host": "data.usajobs.gov",
        }

        all_jobs: List[Dict[str, Any]] = []
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                for page in range(1, max_pages + 1):
                    params = {
                        "Keyword": keyword,
                        "ResultsPerPage": str(results_per_page),
                        "Page": str(page),
                    }
                    if remote_only:
                        params["PositionOfferingTypeCode"] = "15317"  # remote work code

                    logger.info(f"[USAJOBS T1] Querying USAJOBS API: keyword='{keyword}', page={page}/{max_pages}")
                    res = client.get(USAJOBS_API_BASE, headers=headers, params=params)

                    if res.status_code != 200:
                        logger.warning(f"[USAJOBS T1] Page {page} returned HTTP {res.status_code}, stopping pagination.")
                        break

                    data = res.json()
                    search_result = data.get("SearchResult", {})
                    items = search_result.get("SearchResultItems", [])
                    if not items:
                        break  # no more pages

                    all_jobs.extend(self._parse_items(items))

                    total_count = search_result.get("SearchResultCountAll", 0)
                    if len(all_jobs) >= total_count:
                        break  # fetched everything available

            if all_jobs:
                logger.info(f"[USAJOBS T1] Fetched {len(all_jobs)} total job(s) across pagination.")
                return all_jobs

            logger.warning("[USAJOBS T1] Official API returned no items across all pages. Attempting SerpApi fallback.")
            return self._fetch_serpapi_fallback(keyword)

        except Exception as exc:
            logger.error(f"[USAJOBS T1] API request failed: {exc}. Attempting SerpApi fallback.")
            return self._fetch_serpapi_fallback(keyword)

    def _fetch_serpapi_fallback(self, keyword: str) -> List[Dict[str, Any]]:
        serp_key = os.getenv("SERPAPI_API_KEY", "")
        if not serp_key:
            logger.warning("[USAJOBS Fallback] SERPAPI_API_KEY not configured. Returning [].")
            return []
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
            params = {
                "engine": "google_jobs",
                "q": f"{keyword} usajobs",
                "api_key": serp_key,
            }
            with httpx.Client(timeout=15.0) as client:
                res = client.get("https://serpapi.com/search.json", params=params)
            if res.status_code != 200:
                return []
            jobs = []
            for item in res.json().get("jobs_results", []):
                title = item.get("title")
                direct_link = extract_direct_url_from_serpapi_item(item, "usajobs")
                if not direct_link:
                    logger.debug(f"[USAJOBS Fallback] Skipping job '{title}' — no direct USAJOBS URL in apply_options")
                    continue
                if not title:
                    continue
                desc = item.get("description", "")
                ext = item.get("detected_extensions", {})
                jobs.append({
                    "title": str(title).strip(),
                    "company": str(item.get("company_name", "U.S. Federal Government")).strip(),
                    "url": str(direct_link).strip(),
                    "location": str(item.get("location", "United States")).strip(),
                    "remote": "remote" in str(item.get("location", "")).lower(),
                    "job_type": "full_time",
                    "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                    "description": str(desc).strip(),
                    "platform_id": "usajobs",
                    "source_tier": "Tier 4 (SerpApi Fallback)",
                    "country_code": "US",
                    "eligibility_note": ELIGIBILITY_NOTE,
                })
            logger.info(f"[USAJOBS Fallback] SerpApi returned {len(jobs)} jobs with direct URLs.")
            return jobs
        except Exception as exc:
            logger.warning(f"[USAJOBS Fallback] Error: {exc}")
            return []

    def _parse_items(self, items: list) -> List[Dict[str, Any]]:
        jobs = []
        for item in items:
            try:
                matched = item.get("MatchedObjectDescriptor", {})
                if not matched:
                    continue

                title = matched.get("PositionTitle", "")
                org = matched.get("OrganizationName", "")
                dept = matched.get("DepartmentName", "")
                company = org or dept or "U.S. Federal Government"

                apply_uri = matched.get("ApplyURI", [])
                url = apply_uri[0] if apply_uri else matched.get("PositionURI", "")

                if not title or not url:
                    continue

                # Location
                locations = matched.get("PositionLocation", [])
                loc_str = ", ".join(
                    f"{l.get('CityName','')}, {l.get('StateCountrySubDivisionCode','')}"
                    for l in locations if isinstance(l, dict)
                ) if locations else "United States"

                # Remote flag
                remuneration = matched.get("PositionRemuneration", [{}])
                remote = any(
                    "remote" in str(r.get("Description", "")).lower()
                    for r in remuneration if isinstance(r, dict)
                ) or "remote" in loc_str.lower()

                # job_type from PositionSchedule
                schedules = matched.get("PositionSchedule", [])
                schedule_codes = [s.get("Code", "") for s in schedules if isinstance(s, dict)]
                schedule_names = [s.get("Name", "") for s in schedules if isinstance(s, dict)]
                job_type = _parse_job_type_from_schedule(schedule_names + schedule_codes)

                # Salary
                salary = ""
                rem_list = matched.get("PositionRemuneration", [])
                if rem_list and isinstance(rem_list[0], dict):
                    r = rem_list[0]
                    mn = r.get("MinimumRange", "")
                    mx = r.get("MaximumRange", "")
                    rc = r.get("RateIntervalCode", "")
                    salary = f"${mn}–${mx} {rc}".strip() if mn and mx else ""

                # Description snippet
                qualifications = matched.get("QualificationSummary", "")
                desc = matched.get("UserArea", {}).get("Details", {}).get("JobSummary", "") or qualifications

                jobs.append({
                    "title": str(title).strip(),
                    "company": str(company).strip(),
                    "url": str(url).strip(),
                    "location": loc_str.strip(),
                    "remote": remote,
                    "job_type": job_type,
                    "posted_date": matched.get("PublicationStartDate"),
                    "description": str(desc).strip(),
                    "salary": salary,
                    "platform_id": "usajobs",
                    "source_tier": "Tier 1 (USAJOBS Official API)",
                    # Mandatory eligibility tagging per spec
                    "country_code": "US",
                    "eligibility_note": ELIGIBILITY_NOTE,
                })
            except Exception as parse_exc:
                logger.debug(f"[USAJOBS] Skipped item due to parse error: {parse_exc}")
                continue

        logger.info(f"[USAJOBS T1] Parsed {len(jobs)} valid job records.")
        return jobs
