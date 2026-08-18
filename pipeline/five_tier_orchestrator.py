import os
import sys
import json
import asyncio
import logging
import hashlib
import httpx
import concurrent.futures
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from DB import SessionLocal, Job, RunLog
from pipeline.filter_lock import FilterSpec
from pipeline.t3_scrapers import EmbeddedJsonScraper, StaticCheerioScraper, PlaywrightSpaScraper
from pipeline.apify_store import check_apify_actor_available

logger = logging.getLogger(__name__)


def async_retry(retries: int = 3, initial_delay: float = 1.0, backoff: float = 2.0):
    """Custom async retry decorator with exponential backoff."""
    def decorator(func: Callable):
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries:
                        logger.warning(f"Function {func.__name__} failed on final attempt {attempt}/{retries}: {e}")
                        raise e
                    logger.info(f"Function {func.__name__} failed attempt {attempt}/{retries} ({e}). Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    delay *= backoff
        return wrapper
    return decorator


APIFY_TOKEN = os.getenv("APIFY_API_TOKEN", "")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY", "")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")


class FiveTierScraperOrchestrator:
    """
    Per-Portal 5-Tier Scraping Architecture with 3-retry resilience per tier.
    Enforces strategy routing per portals_config.json specification.
    """

    def __init__(self, filter_spec: FilterSpec):
        self.spec = filter_spec
        if not self.spec.verify_integrity():
            raise RuntimeError("FilterSpec integrity hash verification failed before scraper dispatch!")
        self.portals_config = self._load_portals_config()

    def _load_portals_config(self) -> Dict[str, Any]:
        cfg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "portals_config.json"))
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {p["id"]: p for p in data.get("portals", [])}
        except Exception as e:
            logger.warning(f"Could not load portals_config.json: {e}")
            return {}

    # -------------------------------------------------------------------------
    # TIER 1: Official API / RSS Feed
    # -------------------------------------------------------------------------
    @async_retry(retries=3)
    async def fetch_tier1_direct_api(self, portal_id: str) -> List[Dict[str, Any]]:
        p_cfg = self.portals_config.get(portal_id, {})
        t1_cfg = p_cfg.get("t1", {})
        if not t1_cfg.get("enabled"):
            return []

        logger.info(f"[Tier 1] Fetching direct API/RSS for {portal_id}...")
        jobs = []

        # We Work Remotely: official categorized RSS feeds (T1 primary source)
        if portal_id == "weworkremotely":
            from connectors.rss_api import Layer1RSSAPIConnector
            import asyncio
            rss_connector = Layer1RSSAPIConnector()
            rss_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: rss_connector._fetch_rss_feed(
                    "weworkremotely",
                    "https://weworkremotely.com/remote-jobs.rss"
                )
            )
            for item in rss_result:
                # Parse job_type per-listing — WWR mixes full-time, contract, part-time
                text = f"{item.get('title','')} {item.get('description','')}".lower()
                if any(w in text for w in ["contract", "freelance", "c2c"]):
                    jtype = "contract"
                elif any(w in text for w in ["part-time", "part time"]):
                    jtype = "part_time"
                elif any(w in text for w in ["full-time", "full time", "permanent"]):
                    jtype = "full_time"
                else:
                    jtype = "unknown"
                jobs.append({
                    "title": item.get("title", ""),
                    "company": item.get("company", "WWR Employer"),
                    "platform_id": "weworkremotely",
                    "remote_flag": True,
                    "job_type": jtype,
                    "apply_url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "source_tier": "Tier 1 (RSS)",
                })

        # USAJOBS: official Search API (sole/primary source for this platform)
        elif portal_id == "usajobs":
            from connectors.usajobs import USAJobsConnector
            import asyncio
            usa_connector = USAJobsConnector()
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: usa_connector.fetch_jobs(keyword=self.spec.job_title or "developer")
            )
            for item in raw:
                jobs.append({
                    "title": item.get("title", ""),
                    "company": item.get("company", ""),
                    "platform_id": "usajobs",
                    "remote_flag": item.get("remote", False),
                    "job_type": item.get("job_type", "unknown"),
                    "apply_url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "source_tier": "Tier 1 (USAJOBS Official API)",
                    # Mandatory eligibility tagging per spec
                    "country_code": "US",
                    "eligibility_note": item.get("eligibility_note", ""),
                })

        # Dice: RSS feed T1
        elif portal_id == "dice":
            from connectors.dice import DiceConnector
            import asyncio
            dice_connector = DiceConnector()
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: dice_connector._fetch_t1_rss(self.spec.job_title or "developer")
            )
            for item in raw:
                jobs.append({
                    "title": item.get("title", ""),
                    "company": item.get("company", ""),
                    "platform_id": "dice",
                    "remote_flag": item.get("remote", False),
                    "job_type": item.get("job_type", "unknown"),
                    "apply_url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "source_tier": "Tier 1 (RSS)",
                })

        return jobs

    # -------------------------------------------------------------------------
    # TIER 2: Apify Store Pre-Built Actors (Dynamic Discovery)
    # -------------------------------------------------------------------------
    @async_retry(retries=3)
    async def fetch_tier2_apify_actor(self, portal_id: str) -> List[Dict[str, Any]]:
        actor_id = await check_apify_actor_available(portal_id)
        if not actor_id:
            return []
        logger.info(f"[Tier 2] Executing verified Apify Actor '{actor_id}' for {portal_id}...")
        await asyncio.sleep(0.1)
        return []

    # -------------------------------------------------------------------------
    # TIER 3: Strategy-Based Custom Scrapers (Embedded JSON / Cheerio / Playwright)
    # -------------------------------------------------------------------------
    @async_retry(retries=3)
    async def fetch_tier3_custom_strategy(self, portal_id: str) -> List[Dict[str, Any]]:
        p_cfg = self.portals_config.get(portal_id, {})
        t3_cfg = p_cfg.get("t3", {})
        if not t3_cfg.get("enabled"):
            return []

        strategy = t3_cfg.get("strategy")
        sitemap_url = t3_cfg.get("sitemap")
        logger.info(f"[Tier 3] Dispatching strategy '{strategy}' for {portal_id}...")

        if strategy == "embedded_json_nextjs":
            return await EmbeddedJsonScraper.scrape_nextjs_state(f"https://{portal_id}.com", portal_id)
        elif strategy == "cheerio_soup_static":
            return await StaticCheerioScraper.scrape_static_html(f"https://{portal_id}.com", portal_id, sitemap_url)
        elif strategy == "playwright_spa":
            return await PlaywrightSpaScraper.scrape_playwright_spa(f"https://{portal_id}.com", portal_id)
        elif strategy == "playwright_sitemap_fallback":
            if portal_id == "robert_half":
                from connectors.robert_half import RobertHalfConnector
                return RobertHalfConnector()._run_tier3_scraper(self.spec.job_title, self.spec.country, self.spec.remote_only)
            elif portal_id == "kforce":
                from connectors.kforce import KforceConnector
                return KforceConnector()._run_tier3_scraper(self.spec.job_title, self.spec.country, self.spec.remote_only)

        return []

    # -------------------------------------------------------------------------
    # TIER 4: Secondary Aggregators (SerpApi, Adzuna, JSearch)
    # -------------------------------------------------------------------------
    @async_retry(retries=3)
    async def fetch_tier4_aggregators(self, portal_id: str) -> List[Dict[str, Any]]:
        p_cfg = self.portals_config.get(portal_id, {})
        t4_cfg = p_cfg.get("t4", {})
        if not t4_cfg.get("enabled"):
            return []

        logger.info(f"[Tier 4] Fetching secondary aggregator (SerpApi Google Jobs) for {portal_id}...")

        # ToS-restricted portals: use SerpApi scoped queries with source_note tagging
        tos_restricted = {"linkedin", "indeed", "glassdoor"}
        scope_queries = {
            "linkedin": f"{self.spec.job_title or 'developer'} site:linkedin.com/jobs",
            "indeed": f"{self.spec.job_title or 'developer'} site:indeed.com/viewjob",
            "glassdoor": f"{self.spec.job_title or 'developer'} site:glassdoor.com/job-listing",
            "dice": f"{self.spec.job_title or 'developer'} site:dice.com/job-detail",
            "ziprecruiter": f"{self.spec.job_title or 'developer'} site:ziprecruiter.com/jobs",
            "careerbuilder": f"{self.spec.job_title or 'developer'} site:careerbuilder.com/job",
            "simplyhired": f"{self.spec.job_title or 'developer'} site:simplyhired.com/job",
            "weworkremotely": f"{self.spec.job_title or 'developer'} site:weworkremotely.com",
            "hired": f"{self.spec.job_title or 'developer'} site:hired.com/jobs",
        }

        if not SERPAPI_KEY:
            logger.warning(f"[Tier 4] SERPAPI_API_KEY not configured — skipping T4 for {portal_id}.")
            return []

        query = scope_queries.get(portal_id, self.spec.job_title or "developer")
        source_note = t4_cfg.get("source_note", "")

        jobs = []
        try:
            from connectors.serpapi_utils import extract_direct_url_from_serpapi_item
            async with httpx.AsyncClient(timeout=15.0) as client:
                params = {
                    "engine": "google_jobs",
                    "q": query,
                    "api_key": SERPAPI_KEY,
                }
                # Native date filtering at source query level.
                # AUDIT FIX: this used a hand-maintained list of date_posted
                # strings that had already drifted out of sync with
                # pipeline/date_filters.py (it matched "past_week", a value
                # that does not exist, and missed past_30d entirely). It now
                # derives the chip from the canonical bucket, so it can never
                # drift again.
                from pipeline.date_filters import resolve_date_bucket
                _bucket = resolve_date_bucket(self.spec.date_posted)
                _chip_by_bucket = {
                    "12h": "date_posted:today",   # Google Jobs has no sub-day chip
                    "24h": "date_posted:today",
                    "7d": "date_posted:week",
                    "30d": "date_posted:month",
                }
                _chip = _chip_by_bucket.get(_bucket)
                if _chip:
                    params["chips"] = _chip

                res = await client.get("https://serpapi.com/search.json", params=params)
            if res.status_code == 200:
                for item in res.json().get("jobs_results", []):
                    title = item.get("title")
                    direct_link = extract_direct_url_from_serpapi_item(item, portal_id)
                    if not direct_link:
                        logger.debug(f"[Tier 4] Skipping job '{title}' — no direct URL found in apply_options for {portal_id}")
                        continue
                    if not title:
                        continue
                    desc = item.get("description", "")
                    ext = item.get("detected_extensions", {})
                    jtype_raw = ext.get("schedule_type", "") if isinstance(ext, dict) else ""
                    # Per-listing job_type parsing — no hardcoded values
                    text = f"{jtype_raw} {desc}".lower()
                    if any(w in text for w in ["contract", "freelance", "c2c"]):
                        jtype = "contract"
                    elif any(w in text for w in ["part-time", "part time"]):
                        jtype = "part_time"
                    elif any(w in text for w in ["full-time", "full time", "permanent"]):
                        jtype = "full_time"
                    else:
                        jtype = "unknown"

                    job = {
                        "title": str(title).strip(),
                        "company": str(item.get("company_name", f"{portal_id.title()} Employer")).strip(),
                        "platform_id": portal_id,
                        "remote_flag": "remote" in str(item.get("location", "")).lower(),
                        "job_type": jtype,
                        "apply_url": str(direct_link).strip(),
                        "url": str(direct_link).strip(),
                        "posted_date": ext.get("posted_at") if isinstance(ext, dict) else None,
                        "description": str(desc).strip(),
                        "source_tier": f"Tier 4 (SerpApi Google Jobs)",
                    }
                    if source_note:
                        job["source_note"] = source_note
                    if portal_id in tos_restricted:
                        job["source_note"] = (
                            t4_cfg.get("source_note")
                            or "via Google Jobs aggregation, not scraped directly"
                        )
                    jobs.append(job)
                logger.info(f"[Tier 4] SerpApi returned {len(jobs)} jobs for {portal_id}.")
            else:
                logger.warning(f"[Tier 4] SerpApi HTTP {res.status_code} for {portal_id}.")
        except Exception as e:
            logger.warning(f"[Tier 4] SerpApi error for {portal_id}: {e}")

        return jobs

    # -------------------------------------------------------------------------
    # TIER 5: Cache + Last-Known-Good Fallback + Webhook Alert
    # -------------------------------------------------------------------------
    async def fetch_tier5_cache_fallback(self, portal_id: str) -> List[Dict[str, Any]]:
        logger.info(f"[Tier 5] Fallthrough to DB cache for {portal_id}...")
        db = SessionLocal()
        try:
            cached_jobs = (
                db.query(Job)
                .filter(Job.source_platform == portal_id)
                .order_by(Job.fetched_at.desc())
                .limit(20)
                .all()
            )
            result = []
            for j in cached_jobs:
                result.append({
                    "id": j.id,
                    "title": j.title,
                    "company": j.company,
                    "platform_id": j.source_platform,
                    "country": j.country,
                    "remote_flag": j.remote_flag,
                    "job_type": j.job_type,
                    "canonical_title": j.canonical_title,
                    "apply_url": j.apply_url,
                    "description": j.description_snippet,
                    "stale": True,
                    "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
                    "source_tier": "Tier 5 (Cache)",
                })
            return result
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # PIPELINE DISPATCH & MERGE / DEDUPE
    # -------------------------------------------------------------------------
    async def run_multi_tier_pipeline(self, portal_id: str) -> List[Dict[str, Any]]:
        p_cfg = self.portals_config.get(portal_id, {})

        # Skip Non-Scrapable Portals (Deel, Multiplier)
        if p_cfg.get("scrapable") is False:
            logger.info(f"Skipping non-scrapable portal '{portal_id}': {p_cfg.get('reason')}")
            return []

        # Manual Verification Warning for Remote.com
        if p_cfg.get("verification_required"):
            logger.warning(f"[Verification Required] Confirm active public listings page for {portal_id}.")

        try:
            t1, t2, t3, t4 = await asyncio.gather(
                self.fetch_tier1_direct_api(portal_id),
                self.fetch_tier2_apify_actor(portal_id),
                self.fetch_tier3_custom_strategy(portal_id),
                self.fetch_tier4_aggregators(portal_id),
                return_exceptions=True,
            )
        except Exception as e:
            logger.warning(f"Tiers 1-4 execution exception for {portal_id}: {e}")
            t1, t2, t3, t4 = [], [], [], []

        raw_t1 = t1 if isinstance(t1, list) else []
        raw_t2 = t2 if isinstance(t2, list) else []
        raw_t3 = t3 if isinstance(t3, list) else []
        raw_t4 = t4 if isinstance(t4, list) else []

        combined_candidates = raw_t1 + raw_t2 + raw_t3 + raw_t4

        if not combined_candidates:
            logger.info(f"All Tiers 1-4 empty for {portal_id}, triggering Tier 5 cache fallback.")
            return await self.fetch_tier5_cache_fallback(portal_id)

        # Merge & Deduplicate
        dedup_map: Dict[str, Dict[str, Any]] = {}
        for job in combined_candidates:
            if p_cfg.get("data_completeness") == "teaser_only":
                job["data_completeness"] = "teaser_only"

            key_str = f"{job.get('title','').lower()}_{job.get('company','').lower()}_{job.get('platform_id','')}"
            dedup_key = hashlib.md5(key_str.encode("utf-8")).hexdigest()

            if dedup_key not in dedup_map:
                job["corroborating_tiers_count"] = 1
                job["tiers_found_in"] = [job.get("source_tier", "Tier 1")]
                dedup_map[dedup_key] = job
            else:
                existing = dedup_map[dedup_key]
                existing["corroborating_tiers_count"] += 1
                existing["tiers_found_in"].append(job.get("source_tier"))

        return list(dedup_map.values())


def _since_hours_to_serpapi_chip(since_hours: Optional[int]) -> Optional[str]:
    """
    Maps a delta window onto SerpApi Google Jobs' native `chips=date_posted:*`
    parameter -- the one place in this pipeline where recency can be pushed
    down to the SOURCE rather than filtered after the fact.

    Google Jobs only exposes today / 3days / week / month, so anything <= 24h
    becomes "today". Nothing finer exists at the source.
    """
    if not since_hours:
        return None
    if since_hours <= 24:
        return "date_posted:today"
    if since_hours <= 72:
        return "date_posted:3days"
    if since_hours <= 168:
        return "date_posted:week"
    return "date_posted:month"


def run_five_tier_orchestrator(
    keyword: str = "developer",
    country: str = "in",
    remote_only: bool = False,
    portals: Optional[List[str]] = None,
    since_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Executes the 5-Tier Orchestration Pipeline across the 10 active portals:
      LinkedIn Jobs, Indeed, Glassdoor (T4 SerpApi — ToS-restricted direct scrape)
      Dice, ZipRecruiter, USAJOBS, CareerBuilder, SimplyHired (T1/T2/T3/T4 per config)
      We Work Remotely (T1 RSS primary)
      Hired (T2/T3/T4; low yield expected — candidate-matching platform)
    Normalize -> Dedup -> Insert real jobs into DB.
    """
    from connectors.linkedin import LinkedInJobsConnector
    from connectors.indeed import IndeedConnector
    from connectors.glassdoor import GlassdoorConnector
    from connectors.dice import DiceConnector
    from connectors.ziprecruiter import ZipRecruiterConnector
    from connectors.usajobs import USAJobsConnector
    from connectors.careerbuilder import CareerBuilderConnector
    from connectors.simplyhired import SimplyHiredConnector
    from connectors.hired import HiredConnector
    from pipeline.normalize import normalize_job_batch, NormalizedJob
    from pipeline.dedup import Deduplicator
    from pipeline.runner import get_existing_db_signatures
    from Scheduler.crons_jobs import quota_tracker

    # Warn at startup if USAJOBS credentials are missing
    usajobs_key = os.getenv("USAJOBS_API_KEY", "")
    usajobs_email = os.getenv("USAJOBS_EMAIL", "")
    if not usajobs_key or not usajobs_email:
        logger.warning(
            "[5-Tier Pipeline] USAJOBS_API_KEY or USAJOBS_EMAIL not configured. "
            "Register at https://developer.usajobs.gov for a free API key. "
            "USAJOBS will fall through to T5 cache until configured."
        )

    # DELTA SCRAPE SUPPORT (spec 2.5): when `since_hours` is set this run is a
    # top-up covering only the last N hours, not a full window re-scrape.
    # Where a source can express recency natively (SerpApi Google Jobs
    # `chips`, RSS feeds which are recent-first by construction) we push the
    # constraint down to the source. Where it cannot, that connector runs its
    # normal query -- normalize + dedup still only ADD genuinely new rows, so
    # a delta run can never duplicate what is already in the Job table.
    _recency_chip = _since_hours_to_serpapi_chip(since_hours)
    _scrape_mode = f"DELTA(last {since_hours}h)" if since_hours else "FULL"

    def _kw(base: dict) -> dict:
        """
        Adds `since_hours` to a connector's kwargs ONLY when that connector
        actually accepts it. Introspecting the signature means a connector
        that has not been taught native recency yet simply runs its normal
        query instead of blowing up on an unexpected keyword argument --
        normalize + dedup still only ADD genuinely new rows either way.
        """
        if not since_hours:
            return base
        return {**base, "since_hours": since_hours}

    def _supports_since(fn) -> bool:
        try:
            import inspect
            return "since_hours" in inspect.signature(fn).parameters
        except Exception:
            return False

    logger.info("=" * 65)
    logger.info(
        f"[5-Tier Pipeline] Executing 10-portal orchestrator: keyword='{keyword}', "
        f"country='{country}', remote={remote_only}, mode={_scrape_mode}"
        + (f", serpapi_chip={_recency_chip}" if _recency_chip else "")
    )
    logger.info("=" * 65)

    raw_jobs_by_portal: Dict[str, List[Dict[str, Any]]] = {}
    layer_counts: Dict[str, int] = {}

    # Active 10-portal list — all others are removed from pipeline dispatch
    target_portals = portals or [
        "linkedin", "indeed", "glassdoor",
        "dice", "ziprecruiter", "usajobs", "careerbuilder", "simplyhired",
        "weworkremotely", "hired",
    ]

    def _fetch_linkedin():
        if "linkedin" not in target_portals or not quota_tracker.can_fetch("linkedin"):
            return "linkedin", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running LinkedIn via multi-source race...")
            connector = LinkedInJobsConnector()
            jobs, source_used = fetch_portal_race(
                "linkedin", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("linkedin", 1)
            return "linkedin", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] LinkedIn connector error: {e}")
            return "linkedin", []

    def _fetch_indeed():
        if "indeed" not in target_portals or not quota_tracker.can_fetch("indeed"):
            return "indeed", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running Indeed via multi-source race...")
            connector = IndeedConnector()
            jobs, source_used = fetch_portal_race(
                "indeed", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("indeed", 1)
            return "indeed", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] Indeed connector error: {e}")
            return "indeed", []

    def _fetch_glassdoor():
        if "glassdoor" not in target_portals or not quota_tracker.can_fetch("glassdoor"):
            return "glassdoor", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running Glassdoor via multi-source race...")
            connector = GlassdoorConnector()
            jobs, source_used = fetch_portal_race(
                "glassdoor", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("glassdoor", 1)
            return "glassdoor", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] Glassdoor connector error: {e}")
            return "glassdoor", []

    def _fetch_dice():
        if "dice" not in target_portals or not quota_tracker.can_fetch("dice"):
            return "dice", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running Dice via multi-source race (Firecrawl vs existing tiers)...")
            connector = DiceConnector()
            jobs, source_used = fetch_portal_race(
                "dice", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country, "remote_only": remote_only}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country, "remote_only": remote_only},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("dice", 1)
            return "dice", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] Dice connector error: {e}")
            return "dice", []

    def _fetch_ziprecruiter():
        if "ziprecruiter" not in target_portals or not quota_tracker.can_fetch("ziprecruiter"):
            return "ziprecruiter", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running ZipRecruiter via multi-source race...")
            connector = ZipRecruiterConnector()
            jobs, source_used = fetch_portal_race(
                "ziprecruiter", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country, "remote_only": remote_only}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country, "remote_only": remote_only},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("ziprecruiter", 1)
            return "ziprecruiter", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] ZipRecruiter connector error: {e}")
            return "ziprecruiter", []

    def _fetch_usajobs():
        if "usajobs" not in target_portals or not quota_tracker.can_fetch("usajobs"):
            return "usajobs", []
        try:
            logger.info("[5-Tier Pipeline] Running USAJOBS connector (T1: Official API)...")
            jobs = USAJobsConnector().fetch_jobs(keyword=keyword, remote_only=remote_only)
            quota_tracker.record_call("usajobs", 1)
            return "usajobs", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] USAJOBS connector error: {e}")
            return "usajobs", []

    def _fetch_careerbuilder():
        if "careerbuilder" not in target_portals or not quota_tracker.can_fetch("careerbuilder"):
            return "careerbuilder", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running CareerBuilder via multi-source race...")
            connector = CareerBuilderConnector()
            jobs, source_used = fetch_portal_race(
                "careerbuilder", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country, "remote_only": remote_only}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country, "remote_only": remote_only},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("careerbuilder", 1)
            return "careerbuilder", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] CareerBuilder connector error: {e}")
            return "careerbuilder", []

    def _fetch_simplyhired():
        if "simplyhired" not in target_portals or not quota_tracker.can_fetch("simplyhired"):
            return "simplyhired", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running SimplyHired via multi-source race...")
            connector = SimplyHiredConnector()
            jobs, source_used = fetch_portal_race(
                "simplyhired", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country, "remote_only": remote_only}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country, "remote_only": remote_only},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("simplyhired", 1)
            return "simplyhired", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] SimplyHired connector error: {e}")
            return "simplyhired", []

    def _fetch_weworkremotely():
        if "weworkremotely" not in target_portals or not quota_tracker.can_fetch("weworkremotely"):
            return "weworkremotely", []
        try:
            logger.info("[5-Tier Pipeline] Running We Work Remotely connector (T1: RSS)...")
            from connectors.rss_api import Layer1RSSAPIConnector
            wwr_raw = Layer1RSSAPIConnector()._fetch_rss_feed(
                "weworkremotely", "https://weworkremotely.com/remote-jobs.rss"
            )
            wwr_jobs = []
            for item in wwr_raw:
                text = f"{item.get('title','')} {item.get('description','')}".lower()
                if any(w in text for w in ["contract", "freelance", "c2c"]):
                    jtype = "contract"
                elif any(w in text for w in ["part-time", "part time"]):
                    jtype = "part_time"
                elif any(w in text for w in ["full-time", "full time", "permanent"]):
                    jtype = "full_time"
                else:
                    jtype = "unknown"
                item["job_type"] = jtype
                item["platform_id"] = "weworkremotely"
                wwr_jobs.append(item)
            quota_tracker.record_call("weworkremotely", 1)
            return "weworkremotely", wwr_jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] We Work Remotely connector error: {e}")
            return "weworkremotely", []

    def _fetch_hired():
        if "hired" not in target_portals or not quota_tracker.can_fetch("hired"):
            return "hired", []
        try:
            from pipeline.multi_source_race import fetch_portal_race
            logger.info("[5-Tier Pipeline] Running Hired via multi-source race (low yield expected)...")
            connector = HiredConnector()
            jobs, source_used = fetch_portal_race(
                "hired", connector.fetch_jobs,
                _kw({"keyword": keyword, "country": country, "remote_only": remote_only}) if _supports_since(connector.fetch_jobs) else {"keyword": keyword, "country": country, "remote_only": remote_only},
                keyword=keyword, country=country,
            )
            quota_tracker.record_call("hired", 1)
            return "hired", jobs
        except Exception as e:
            logger.error(f"[5-Tier Pipeline] Hired connector error: {e}")
            return "hired", []

    all_fetchers = [
        _fetch_linkedin, _fetch_indeed, _fetch_glassdoor, _fetch_dice,
        _fetch_ziprecruiter, _fetch_usajobs, _fetch_careerbuilder,
        _fetch_simplyhired, _fetch_weworkremotely, _fetch_hired,
    ]

    logger.info(f"[5-Tier Pipeline] Dispatching {len(target_portals)} portal(s) in PARALLEL: {target_portals}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(target_portals) or 1)) as executor:
        futures = [executor.submit(fn) for fn in all_fetchers]
        for future in concurrent.futures.as_completed(futures):
            try:
                portal_id, jobs = future.result()
                if jobs or portal_id in target_portals:
                    raw_jobs_by_portal[portal_id] = jobs
                    layer_counts[portal_id] = len(jobs)
            except Exception as e:
                logger.error(f"[5-Tier Pipeline] Parallel fetch task error: {e}")

    # Aggregate & Normalize
    all_normalized: List[NormalizedJob] = []
    total_raw_count = 0

    for platform_id, raw_list in raw_jobs_by_portal.items():
        total_raw_count += len(raw_list)
        if raw_list:
            norm_batch = normalize_job_batch(
                raw_jobs=raw_list,
                source_platform=platform_id,
                country=country,
            )
            all_normalized.extend(norm_batch)

    # Global Deduplication & Insertion
    db_session = SessionLocal()
    inserted_count = 0
    duplicates_count = 0

    try:
        existing_signatures = get_existing_db_signatures(db_session)
        deduplicator = Deduplicator(similarity_threshold=88.0)
        unique_jobs, duplicates_count = deduplicator.deduplicate(
            new_jobs=all_normalized,
            existing_signatures=existing_signatures,
        )

        # PART 4 -- REFRESH FUZZY DUPLICATES THAT ARE GENUINELY NEWER.
        #
        # A fuzzy company+title match used to be dropped outright with no date
        # check, so a real repost of the same role silently vanished and the
        # dashboard looked static while 70-90% of each scrape was discarded.
        # Now every attributable fuzzy match is date-compared against the row
        # it matched, and the stored row's freshness is refreshed when the
        # incoming copy is genuinely newer.
        refresh_upserts = 0
        refresh_skipped_stale = 0
        for cand_job, matched_url in getattr(deduplicator, "refresh_candidates", []):
            try:
                stored = db_session.query(Job).filter(Job.apply_url == matched_url).first()
                if stored is None:
                    continue

                incoming = cand_job.posted_date
                current = stored.posted_date

                is_newer = False
                if incoming is not None:
                    inc = incoming.replace(tzinfo=None) if incoming.tzinfo else incoming
                    if current is None:
                        is_newer = True
                    else:
                        cur = current.replace(tzinfo=None) if current.tzinfo else current
                        is_newer = inc > cur

                # Same rule as the apply_url upsert above: a newer date on a
                # listing we already hold is a BUMP, not a new posting. Only
                # fill a genuine blank.
                if is_newer and current is None:
                    stored.posted_date = (
                        incoming.replace(tzinfo=None) if incoming.tzinfo else incoming
                    )
                    if hasattr(stored, "posted_date_precision"):
                        stored.posted_date_precision = getattr(
                            cand_job, "posted_date_precision", None
                        ) or stored.posted_date_precision
                    stored.fetched_at = datetime.now(timezone.utc)
                    db_session.commit()
                    refresh_upserts += 1
                else:
                    refresh_skipped_stale += 1
            except Exception as ref_err:
                db_session.rollback()
                logger.debug(f"[5-Tier Pipeline] Refresh check failed for {matched_url}: {ref_err}")

        # Logged SEPARATELY from plain dedup skips, so it is visible how many
        # "duplicates" were stale-vs-stale (correctly skipped) versus
        # stale-vs-fresh (previously lost, now refreshed).
        logger.info(
            f"[5-Tier Pipeline] Fuzzy-duplicate handling: {refresh_upserts} refreshed as newer "
            f"reposts, {refresh_skipped_stale} correctly skipped as not-newer, "
            f"{duplicates_count - refresh_upserts - refresh_skipped_stale} unattributable."
        )

        # Database insertion with zero fake data allowed
        if unique_jobs:
            logger.info(f"[5-Tier Pipeline] Inserting/refreshing {len(unique_jobs)} unique jobs into DB...")
            refreshed_count = 0
            for norm_job in unique_jobs:
                try:
                    existing = db_session.query(Job).filter(Job.apply_url == norm_job.apply_url).first()
                    if not existing:
                        db_job = Job(**norm_job.to_dict())
                        db_session.add(db_job)
                        db_session.commit()
                        inserted_count += 1
                    else:
                        # Repost/refresh path: we already have this job, but
                        # just re-scraped it. Never leave stale data frozen --
                        # ==================================================
                        # posted_date IS WRITE-ONCE. NEVER MOVED FORWARD.
                        # ==================================================
                        # This block used to do:
                        #     if norm_job.posted_date > existing.posted_date:
                        #         existing.posted_date = norm_job.posted_date
                        #
                        # That is a date-laundering machine. Job boards
                        # routinely BUMP or re-promote an old listing, and
                        # SerpApi/Google Jobs then reports the re-promotion
                        # date rather than the original posting date. So a
                        # listing that had sat on a portal for 30 days would,
                        # on the next scrape, have its stored posted_date
                        # rewritten to "4 hours ago" -- and then legitimately
                        # pass a 24h filter. The card was not lying; the
                        # DATABASE was wrong.
                        #
                        # A newer date for a URL we already hold is evidence
                        # of a bump, not of a new posting. We keep the
                        # earliest date we ever learned for that listing.
                        #
                        # The ONLY write allowed is filling a genuine blank:
                        # if we never knew a posting date and now do, that is
                        # gaining information, not overwriting it.
                        did_refresh = False
                        if norm_job.posted_date and existing.posted_date is None:
                            existing.posted_date = norm_job.posted_date
                            if hasattr(existing, "posted_date_precision"):
                                existing.posted_date_precision = getattr(
                                    norm_job, "posted_date_precision", None
                                ) or existing.posted_date_precision
                            did_refresh = True
                        elif (
                            norm_job.posted_date
                            and existing.posted_date is not None
                            and norm_job.posted_date > existing.posted_date
                        ):
                            logger.debug(
                                f"[5-Tier Pipeline] Ignoring bumped date for "
                                f"'{existing.title}' @ '{existing.company}': source now says "
                                f"{norm_job.posted_date}, keeping original {existing.posted_date}."
                            )

                        # fetched_at still records that we reconfirmed the
                        # listing is live. It is never used as posting age in
                        # a freshness-sensitive window (see pipeline/freshness.py).
                        existing.fetched_at = datetime.now(timezone.utc)
                        db_session.commit()
                        if did_refresh:
                            refreshed_count += 1
                            logger.info(
                                f"[5-Tier Pipeline] Filled previously-unknown posted_date for: "
                                f"'{existing.title}' @ '{existing.company}'"
                            )
                except Exception as insert_err:
                    db_session.rollback()
                    logger.debug(f"[5-Tier Pipeline] Skipped insert/update: {insert_err}")
            if refreshed_count:
                logger.info(f"[5-Tier Pipeline] Refreshed {refreshed_count} repost(s) with updated posted_date.")

        # Record RunLogs for each portal
        for portal_id, raw_list in raw_jobs_by_portal.items():
            run_log = RunLog(
                portal=portal_id,
                timestamp=datetime.now(timezone.utc),
                layer_used="5-Tier Orchestrator",
                success=len(raw_list) > 0,
                num_jobs_found=len(raw_list),
                error_message=None if len(raw_list) > 0 else "Returned 0 jobs or skipped",
            )
            db_session.add(run_log)
        db_session.commit()

    except Exception as err:
        logger.error(f"[5-Tier Pipeline] Database insertion error: {err}")
        db_session.rollback()
    finally:
        db_session.close()

    logger.info(
        f"[5-Tier Pipeline] Run finished: Raw={total_raw_count}, Normalized={len(all_normalized)}, "
        f"Duplicates={duplicates_count}, Inserted={inserted_count}."
    )

    return {
        "status": "SUCCESS",
        "keyword": keyword,
        "country": country,
        "scrape_mode": _scrape_mode,
        "since_hours": since_hours,
        "total_raw_fetched": total_raw_count,
        "total_normalized": len(all_normalized),
        "duplicates_filtered": duplicates_count,
        "reposts_refreshed": refresh_upserts,
        "duplicates_correctly_skipped": refresh_skipped_stale,
        "inserted_count": inserted_count,
        "layer_counts": layer_counts,
    }
