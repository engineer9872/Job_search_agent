import logging
import httpx
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional

try:
    import feedparser
except ImportError:
    feedparser = None

logger = logging.getLogger(__name__)


class Layer1RSSAPIConnector:
    """
    Layer 1 Ingestion: Official REST APIs & RSS Feeds (RemoteOK, Remotive, WWR, WorkingNomads).
    Fast, stable, and 100% ToS-compliant.
    """

    def fetch_portal_data(self, portal_config: Dict[str, Any], keyword: str = "developer", country: str = "us") -> List[Dict[str, Any]]:
        """
        Fetches job records using official Layer 1 REST API or RSS Feed, or dispatches to dedicated connector.
        """
        portal_id = portal_config.get("id")
        layer1_cfg = portal_config.get("layer1", {})

        if portal_id == "linkedin":
            from connectors.linkedin import LinkedInJobsConnector
            return LinkedInJobsConnector().fetch_jobs(keyword=keyword, country=country, page=1)
        elif portal_id == "naukri":
            from connectors.naukri import NaukriConnector
            return NaukriConnector().fetch_jobs(keyword=keyword, country=country, page=1)
        elif portal_id == "indeed":
            from connectors.indeed import IndeedConnector
            return IndeedConnector().fetch_jobs(keyword=keyword, country=country, page=1)
        elif portal_id == "glassdoor":
            from connectors.glassdoor import GlassdoorConnector
            return GlassdoorConnector().fetch_jobs(keyword=keyword, country=country, page=1)

        url = layer1_cfg.get("url")
        feed_type = layer1_cfg.get("type", "api")

        if not url:
            raise ValueError(f"Portal '{portal_id}' has no Layer 1 URL configured.")

        if feed_type == "rss":
            return self._fetch_rss_feed(portal_id, url)
        else:
            return self._fetch_json_api(portal_id, url)

    def _fetch_json_api(self, portal_id: str, url: str) -> List[Dict[str, Any]]:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            res = client.get(url)
            if res.status_code != 200:
                raise RuntimeError(f"Layer 1 REST API returned HTTP {res.status_code} for '{url}'")

            data = res.json()
            raw_jobs = []

            if portal_id == "remoteok":
                # RemoteOK returns list where index 0 is legal notice
                items = data[1:] if isinstance(data, list) and len(data) > 1 else []
                for item in items:
                    if isinstance(item, dict):
                        raw_jobs.append({
                            "title": item.get("position"),
                            "company": item.get("company"),
                            "url": item.get("url") or f"https://remoteok.com/l/{item.get('id')}",
                            "location": item.get("location") or "Remote",
                            "remote": True,
                            "contract_type": "contract",
                            "posted_date": item.get("date"),
                            "description": item.get("description"),
                        })
            elif portal_id == "remotive":
                items = data.get("jobs", []) if isinstance(data, dict) else []
                for item in items:
                    if isinstance(item, dict):
                        raw_jobs.append({
                            "title": item.get("title"),
                            "company": item.get("company_name") or item.get("company"),
                            "url": item.get("url") or item.get("link"),
                            "location": item.get("candidate_required_location") or item.get("location") or "Remote",
                            "remote": True,
                            "contract_type": str(item.get("job_type", "contract")),
                            "posted_date": item.get("publication_date") or item.get("sub_date"),
                            "description": item.get("description"),
                        })
            return raw_jobs

    def _fetch_rss_feed(self, portal_id: str, url: str) -> List[Dict[str, Any]]:
        if feedparser is not None:
            try:
                feed = feedparser.parse(url)
                if not feed.bozo or feed.entries:
                    raw_jobs = []
                    for entry in feed.entries:
                        title = entry.get("title", "")
                        company = "Remote Partner"
                        if " at " in title:
                            parts = title.split(" at ", 1)
                            title = parts[0].strip()
                            company = parts[1].strip()
                        elif ":" in title:
                            parts = title.split(":", 1)
                            company = parts[0].strip()
                            title = parts[1].strip()

                        raw_jobs.append({
                            "title": title,
                            "company": company,
                            "url": entry.get("link"),
                            "location": "Remote",
                            "remote": True,
                            "contract_type": "contract",
                            "posted_date": entry.get("published") or entry.get("updated"),
                            "description": entry.get("summary") or entry.get("description"),
                        })
                    return raw_jobs
            except Exception as e:
                logger.warning(f"feedparser failed for '{url}': {e}")

        # Standard library xml.etree.ElementTree fallback
        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(url)
                if res.status_code == 200:
                    root = ET.fromstring(res.content)
                    raw_jobs = []
                    for item in root.findall(".//item"):
                        title_t = item.findtext("title") or ""
                        link_t = item.findtext("link") or ""
                        desc_t = item.findtext("description") or ""
                        pub_t = item.findtext("pubDate") or ""

                        company = "Remote Partner"
                        if " at " in title_t:
                            parts = title_t.split(" at ", 1)
                            title_t = parts[0].strip()
                            company = parts[1].strip()

                        raw_jobs.append({
                            "title": title_t,
                            "company": company,
                            "url": link_t,
                            "location": "Remote",
                            "remote": True,
                            "contract_type": "contract",
                            "posted_date": pub_t,
                            "description": desc_t,
                        })
                    return raw_jobs
        except Exception as e:
            logger.warning(f"Standard library RSS XML parsing failed for '{url}': {e}")

        return []
