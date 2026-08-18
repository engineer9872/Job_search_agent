"""
JSON-LD Structured Data Harvester (Method 4)
Parses schema.org/JobPosting JSON-LD blocks embedded in public job pages HTML.
Fast, lightweight, structured parsing without full browser automation.
"""

import json
import logging
import httpx
from typing import Dict, Any, List, Optional
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


class JSONLDHarvester:
    """
    Harvester for schema.org/JobPosting JSON-LD structured data script blocks.
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def extract_job_postings_from_html(self, html_content: str, page_url: str) -> List[Dict[str, Any]]:
        """
        Parses HTML string to find all <script type="application/ld+json"> blocks
        and returns standardized raw job dicts for any JobPosting schema found.
        """
        jobs = []
        if not BeautifulSoup or not html_content:
            return jobs

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            scripts = soup.find_all("script", type="application/ld+json")

            for script in scripts:
                if not script.string:
                    continue
                try:
                    data = json.loads(script.string)
                    items = data if isinstance(data, list) else [data]

                    for item in items:
                        if isinstance(item, dict):
                            # Handle @graph or direct JobPosting
                            graph = item.get("@graph")
                            target_objects = graph if isinstance(graph, list) else [item]

                            for obj in target_objects:
                                if not isinstance(obj, dict):
                                    continue
                                type_name = str(obj.get("@type", "")).lower()
                                if "jobposting" in type_name:
                                    title = obj.get("title") or obj.get("name")
                                    company = obj.get("hiringOrganization", {})
                                    company_name = company.get("name") if isinstance(company, dict) else str(company)
                                    location = obj.get("jobLocation", {})
                                    loc_str = ""
                                    if isinstance(location, dict):
                                        addr = location.get("address", {})
                                        if isinstance(addr, dict):
                                            loc_str = addr.get("addressLocality") or addr.get("addressRegion") or addr.get("addressCountry", "")
                                        elif isinstance(addr, str):
                                            loc_str = addr
                                    elif isinstance(location, list):
                                        loc_str = ", ".join([str(l) for l in location if l])

                                    link = obj.get("url") or page_url

                                    if title and company_name:
                                        jobs.append({
                                            "title": str(title).strip(),
                                            "company": str(company_name).strip(),
                                            "url": str(link).strip(),
                                            "location": str(loc_str or "Remote").strip(),
                                            "posted_date": obj.get("datePosted"),
                                            "description": str(obj.get("description", "")).strip(),
                                            "contract_type": str(obj.get("employmentType") or "full_time"),
                                            "source_method": "jsonld_harvesting",
                                        })
                except Exception as parse_err:
                    logger.debug(f"JSON-LD script parse error: {parse_err}")

        except Exception as err:
            logger.warning(f"Error parsing HTML for JSON-LD at {page_url}: {err}")

        return jobs

    def fetch_url_jsonld(self, target_url: str, source_platform: str = "jsonld") -> List[Dict[str, Any]]:
        """
        Fetches a target URL and extracts schema.org/JobPosting structured data.
        """
        headers = {
            "User-Agent": USER_AGENTS[0],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                res = client.get(target_url)
                if res.status_code == 200:
                    jobs = self.extract_job_postings_from_html(res.text, target_url)
                    for j in jobs:
                        j["source_platform"] = source_platform
                    return jobs
        except Exception as exc:
            logger.warning(f"JSONLDHarvester failed for '{target_url}': {exc}")

        return []
