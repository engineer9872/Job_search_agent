import json
import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from DB import SessionLocal, Job, RunLog
from connectors.rss_api import Layer1RSSAPIConnector
from connectors.apify_store import Layer2ApifyStoreConnector
from scrapers.custom_playwright import Layer3CustomScraper
from alerts.webhook import send_fallback_alert
from pipeline.normalize import extract_city_and_country, detect_remote, parse_date

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "portals_config.json")


def compute_job_hash(title: str, company: str, portal: str, posted_date: Optional[str] = None) -> str:
    """
    Computes a deterministic SHA256 hash of (title + company + portal + posted_date) for deduplication.
    """
    raw_str = f"{title.strip().lower()}|{company.strip().lower()}|{portal.strip().lower()}|{str(posted_date).strip().lower()}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def load_portals_config() -> List[Dict[str, Any]]:
    """Loads configuration for all portals from config/portals_config.json."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Config file not found at '{CONFIG_PATH}'")
        return []
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("portals", [])


class RemoteContractPipeline:
    """
    Unified 4-Layer Fallback Ingestion Pipeline.
    Runs Layers 1 -> 2 -> 3 -> 4 sequentially per portal.
    """

    def __init__(self):
        self.layer1_connector = Layer1RSSAPIConnector()
        self.layer2_connector = Layer2ApifyStoreConnector()
        self.layer3_scraper = Layer3CustomScraper()

    def run_portal_ingestion(self, portal_config: Dict[str, Any], keyword: str = "developer", country: str = "us") -> Dict[str, Any]:
        """
        Executes 4-layer fallback pipeline for a single portal.
        """
        portal_id = portal_config.get("id")
        portal_name = portal_config.get("name")
        layer_used = "Layer 4"
        success = False
        error_msg = None
        raw_jobs = []

        db = SessionLocal()

        # -------------------------------------------------------------------
        # LAYER 1: Official REST API / RSS Feed
        # -------------------------------------------------------------------
        if portal_config.get("layer1", {}).get("enabled"):
            try:
                raw_jobs = self.layer1_connector.fetch_portal_data(portal_config, keyword=keyword, country=country)
                if raw_jobs:
                    layer_used = "Layer 1"
                    success = True
            except Exception as e:
                logger.warning(f"[Pipeline] Layer 1 failed for '{portal_id}': {e}")
                error_msg = f"Layer 1 Error: {e}"

        # -------------------------------------------------------------------
        # LAYER 2: Apify Store Community Actor
        # -------------------------------------------------------------------
        if not success and portal_config.get("layer2", {}).get("enabled"):
            try:
                raw_jobs = self.layer2_connector.fetch_portal_data(portal_config, keyword=keyword, country=country)
                if raw_jobs:
                    layer_used = "Layer 2"
                    success = True
            except Exception as e:
                logger.warning(f"[Pipeline] Layer 2 failed for '{portal_id}': {e}")
                error_msg = f"Layer 2 Error: {e}"

        # -------------------------------------------------------------------
        # LAYER 3: Custom Playwright / Cheerio Scraper
        # -------------------------------------------------------------------
        if not success and portal_config.get("layer3", {}).get("enabled"):
            try:
                raw_jobs = self.layer3_scraper.fetch_portal_data(portal_config, keyword=keyword, country=country)
                if raw_jobs:
                    layer_used = "Layer 3"
                    success = True
            except Exception as e:
                logger.warning(f"[Pipeline] Layer 3 failed for '{portal_id}': {e}")
                error_msg = f"Layer 3 Error: {e}"

        # -------------------------------------------------------------------
        # LAYER 4: Cached Last-Good Data + Alerting Trigger
        # -------------------------------------------------------------------
        if not success:
            layer_used = "Layer 4"
            logger.error(f"[Pipeline] Layer 1, 2, and 3 failed for '{portal_name}'. Invoking Layer 4 Fallback.")
            send_fallback_alert(portal_name, layer_failed="Layers 1-3", failure_reason=error_msg or "All live layers unreachable")

            # Fetch cached records from database
            cached_jobs = db.query(Job).filter(Job.source_platform == portal_id).limit(10).all()
            num_jobs = len(cached_jobs)
        else:
            num_jobs = self._process_and_store_jobs(db, raw_jobs, portal_id, country)

        # Record health execution log in run_logs table
        run_log = RunLog(
            portal=portal_id,
            timestamp=datetime.now(timezone.utc),
            layer_used=layer_used,
            success=success,
            num_jobs_found=num_jobs,
            error_message=error_msg,
        )
        db.add(run_log)
        db.commit()
        db.close()

        return {
            "portal": portal_id,
            "layer_used": layer_used,
            "success": success,
            "num_jobs": num_jobs,
            "error": error_msg,
        }

    def _process_and_store_jobs(self, db, raw_jobs: List[Dict[str, Any]], portal_id: str, default_country: str) -> int:
        """
        Normalizes, deduplicates via raw_hash and apply_url, and stores job listings safely.
        """
        inserted_count = 0
        seen_urls = set()
        seen_hashes = set()

        for raw in raw_jobs:
            title = str(raw.get("title", "")).strip()
            company = str(raw.get("company", "Remote Client")).strip()
            apply_url = str(raw.get("url", "")).strip()
            if not title or not apply_url:
                continue

            if apply_url in seen_urls:
                continue

            posted_date = parse_date(raw.get("posted_date"))
            raw_hash = compute_job_hash(title, company, portal_id, str(posted_date))

            if raw_hash in seen_hashes:
                continue

            # Deduplication Check against DB
            existing = db.query(Job).filter((Job.raw_hash == raw_hash) | (Job.apply_url == apply_url)).first()
            if existing:
                seen_urls.add(apply_url)
                seen_hashes.add(raw_hash)
                continue

            city, country = extract_city_and_country(raw.get("location"), default_country=default_country.upper())
            remote_flag = raw.get("remote", True) or detect_remote(title, raw.get("description", ""), str(raw.get("location")))

            new_job = Job(
                title=title[:255],
                company=company[:255],
                city=city[:100] if city else None,
                country=country[:100] if country else default_country.upper(),
                salary_min=None,
                salary_max=None,
                currency=None,
                remote_flag=remote_flag,
                job_type=str(raw.get("contract_type", "contract"))[:50],
                source_platform=portal_id,
                apply_url=apply_url,
                description_snippet=str(raw.get("description", ""))[:2000] if raw.get("description") else None,
                posted_date=posted_date,
                fetched_at=datetime.now(timezone.utc),
                raw_hash=raw_hash,
                contract_type=str(raw.get("contract_type", "contract"))[:50],
                scraped_at=datetime.now(timezone.utc),
            )

            try:
                db.add(new_job)
                db.commit()
                seen_urls.add(apply_url)
                seen_hashes.add(raw_hash)
                inserted_count += 1
            except Exception as e:
                db.rollback()
                logger.debug(f"[Pipeline] Skipped duplicate job for '{portal_id}': {e}")

        return inserted_count



def run_4layer_pipeline(target_portals: Optional[List[str]] = None, keyword: str = "developer", country: str = "us") -> List[Dict[str, Any]]:
    """
    Main entry point for running the 4-layer fallback pipeline across portals.
    """
    configs = load_portals_config()
    if target_portals:
        configs = [c for c in configs if c.get("id") in target_portals]

    pipeline = RemoteContractPipeline()
    results = []
    for cfg in configs:
        res = pipeline.run_portal_ingestion(cfg, keyword=keyword, country=country)
        results.append(res)
    return results
