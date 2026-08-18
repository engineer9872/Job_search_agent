import os
import sys
import logging
from dotenv import load_dotenv

# Ensure root workspace directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Populate10Portals")

from DB import init_db, SessionLocal, Job
from pipeline.normalize import normalize_connector_job, normalize_job_batch
from pipeline.dedup import Deduplicator
import httpx

SERPAPI_KEY = os.getenv("SERPAPI_API_KEY", "")

ACTIVE_PORTALS = [
    "linkedin", "indeed", "glassdoor", "dice", "ziprecruiter",
    "usajobs", "careerbuilder", "simplyhired", "weworkremotely", "hired"
]


def fetch_serpapi_jobs_for_portal(portal_id: str, keyword: str = "developer") -> list:
    """Fetch jobs from SerpApi Google Jobs scoped to portal_id."""
    if not SERPAPI_KEY:
        return []
    
    query_map = {
        "linkedin": f"{keyword} linkedin",
        "indeed": f"{keyword} indeed",
        "glassdoor": f"{keyword} glassdoor",
        "dice": f"{keyword} dice",
        "ziprecruiter": f"{keyword} ziprecruiter",
        "usajobs": f"{keyword} usajobs",
        "careerbuilder": f"{keyword} careerbuilder",
        "simplyhired": f"{keyword} simplyhired",
        "hired": f"{keyword} hired",
    }
    q = query_map.get(portal_id, f"{keyword} {portal_id}")
    params = {"engine": "google_jobs", "q": q, "api_key": SERPAPI_KEY}
    
    try:
        res = httpx.get("https://serpapi.com/search.json", params=params, timeout=15.0)
        if res.status_code != 200:
            return []
        items = res.json().get("jobs_results", [])
        jobs = []
        for item in items:
            title = item.get("title")
            link = (
                item.get("related_links", [{}])[0].get("link")
                or item.get("share_link") or item.get("link")
            )
            if not title or not link:
                continue
            desc = item.get("description", "")
            ext = item.get("detected_extensions", {})
            jobs.append({
                "title": str(title).strip(),
                "company": str(item.get("company_name", f"{portal_id.title()} Employer")).strip(),
                "url": str(link).strip(),
                "location": str(item.get("location", "Remote")).strip(),
                "remote": "remote" in str(item.get("location", "")).lower(),
                "job_type": "full_time",
                "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                "description": str(desc).strip(),
                "platform_id": portal_id,
            })
        return jobs
    except Exception as e:
        logger.warning(f"[SerpApi] Error for {portal_id}: {e}")
        return []


def fetch_wwr_rss() -> list:
    """Fetch jobs from We Work Remotely RSS feed."""
    try:
        from connectors.rss_api import Layer1RSSAPIConnector
        raw = Layer1RSSAPIConnector()._fetch_rss_feed("weworkremotely", "https://weworkremotely.com/remote-jobs.rss")
        jobs = []
        for item in raw:
            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company", "WWR Employer"),
                "url": item.get("url", ""),
                "location": "Remote",
                "remote": True,
                "job_type": "full_time",
                "description": item.get("description", ""),
                "platform_id": "weworkremotely",
            })
        return jobs
    except Exception as e:
        logger.warning(f"[WWR RSS] Error: {e}")
        return []


def populate_all_10_portals():
    logger.info("Starting database population for ALL 10 portals...")
    init_db()
    db = SessionLocal()

    keywords = ["software engineer", "python developer", "react developer", "data engineer"]
    summary = {}

    for portal_id in ACTIVE_PORTALS:
        logger.info(f"=== Processing portal: {portal_id} ===")
        raw_jobs = []

        if portal_id == "weworkremotely":
            raw_jobs = fetch_wwr_rss()
        else:
            for kw in keywords:
                batch = fetch_serpapi_jobs_for_portal(portal_id, keyword=kw)
                raw_jobs.extend(batch)
                if len(raw_jobs) >= 25:
                    break

        logger.info(f"[{portal_id}] Total raw jobs fetched: {len(raw_jobs)}")

        if raw_jobs:
            normalized = normalize_job_batch(raw_jobs, source_platform=portal_id, country="us")
            deduper = Deduplicator(similarity_threshold=88.0)
            unique_jobs, _ = deduper.deduplicate(normalized)

            count = 0
            for norm_job in unique_jobs:
                try:
                    job_dict = norm_job.to_dict()
                    job_dict["source_platform"] = portal_id
                    db_job = Job(**job_dict)
                    db.add(db_job)
                    db.commit()
                    count += 1
                except Exception as ex:
                    db.rollback()
            summary[portal_id] = count
            logger.info(f"[{portal_id}] Inserted {count} unique jobs into DB.")
        else:
            summary[portal_id] = 0

    db.close()
    logger.info(f"FINAL POPULATION SUMMARY ACROSS ALL 10 PORTALS:\n{summary}")

if __name__ == "__main__":
    populate_all_10_portals()
