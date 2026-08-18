import os
import json
import logging
import datetime
from typing import Dict, Any, List, Optional
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# Daily free-tier API quotas per platform (reflecting monthly free-tier caps / 30 days)
DEFAULT_DAILY_QUOTAS = {
    "linkedin": 150,
    "indeed": 150,
    "glassdoor": 150,
    "dice": 150,
    "ziprecruiter": 150,
    "usajobs": 500,
    "careerbuilder": 150,
    "simplyhired": 150,
    "weworkremotely": 500,
    "hired": 150,
}


STATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "quota_tracker.json")


class RateQuotaTracker:
    """
    Per-source daily API-call counter with file-backed persistence across server restarts.
    Guards against exceeding API free-tier quotas.
    """

    def __init__(self, quotas: Optional[Dict[str, int]] = None, state_file: str = STATE_FILE_PATH):
        self.quotas = quotas or DEFAULT_DAILY_QUOTAS.copy()
        self.state_file = state_file
        self.current_day: str = datetime.date.today().isoformat()
        self.counts: Dict[str, int] = {k: 0 for k in self.quotas}
        self._load_state()

    def _load_state(self):
        """Loads count state from persistent JSON file if date matches today."""
        today = datetime.date.today().isoformat()
        self.current_day = today

        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    saved_date = data.get("date")
                    saved_counts = data.get("counts", {})

                    if saved_date == today:
                        # Resume today's quota usage across server restart
                        for k in self.quotas:
                            self.counts[k] = saved_counts.get(k, 0)
                        logger.info(f"[QuotaTracker] Loaded persistent quota state for '{today}': {self.counts}")
                        return
                    else:
                        logger.info(f"[QuotaTracker] Resetting quota counters for new date '{today}' (previous: '{saved_date}').")
            except Exception as e:
                logger.warning(f"[QuotaTracker] Failed to read state file '{self.state_file}': {e}")

        # Reset counts for new day or missing file
        self.counts = {k: 0 for k in self.quotas}
        self._save_state()

    def _save_state(self):
        """Persists current date and call counts to JSON file."""
        try:
            data = {
                "date": self.current_day,
                "counts": self.counts,
                "quotas": self.quotas,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[QuotaTracker] Failed to save state file '{self.state_file}': {e}")

    def _check_day_reset(self):
        today = datetime.date.today().isoformat()
        if today != self.current_day:
            logger.info(f"[QuotaTracker] New day detected ('{today}'). Resetting daily API call counters.")
            self.current_day = today
            self.counts = {k: 0 for k in self.quotas}
            self._save_state()

    def can_fetch(self, source: str) -> bool:
        """
        Checks if the daily call limit for the given source has been reached.
        """
        self._check_day_reset()
        src = source.lower().strip()
        limit = self.quotas.get(src, 1000)
        current = self.counts.get(src, 0)

        if current >= limit:
            logger.warning(
                f"[QuotaTracker] Daily API quota limit reached for '{src}' ({current}/{limit}). "
                f"Skipping fetch for this source today."
            )
            return False
        return True

    def record_call(self, source: str, count: int = 1):
        """
        Records API calls made for a given source and persists state to file.
        """
        self._check_day_reset()
        src = source.lower().strip()
        self.counts[src] = self.counts.get(src, 0) + count
        self._save_state()
        limit = self.quotas.get(src, 1000)
        logger.info(f"[QuotaTracker] Recorded {count} call(s) for '{src}'. Today's Usage: {self.counts[src]}/{limit}")

    def get_status(self) -> Dict[str, Any]:
        """
        Returns quota tracking status dictionary.
        """
        self._check_day_reset()
        return {
            "date": self.current_day,
            "quotas": self.quotas,
            "usage": self.counts,
            "persisted_file": self.state_file,
        }


# Global instances
quota_tracker = RateQuotaTracker()
scheduler: Optional[BackgroundScheduler] = None


def scheduled_pipeline_job():
    """
    Cron task wrapper that checks daily quota guardrails before triggering pipeline execution (Pipeline A).
    """
    logger.info("[Scheduler] Cron job triggered: Scheduled Multi-Source Ingestion.")
    try:
        from pipeline import run_pipeline
        all_sources = ["adzuna", "jsearch", "google_jobs", "remotive", "greenhouse", "lever"]
        allowed_sources = []

        for src in all_sources:
            if quota_tracker.can_fetch(src):
                allowed_sources.append(src)
                quota_tracker.record_call(src, 1)

        if not allowed_sources:
            logger.warning("[Scheduler] All source API daily quotas exhausted. Skipping pipeline run.")
            return

        logger.info(f"[Scheduler] Running pipeline for allowed sources: {allowed_sources}")
        run_pipeline(
            sources=allowed_sources,
            country="us",
            keyword="python developer",
            pages=1,
        )
    except Exception as e:
        logger.error(f"[Scheduler] Scheduled pipeline execution error: {e}", exc_info=True)


# Rotates through real search terms users actually filter by, instead of
# a single static keyword -- this is what keeps "past 24h" searches populated
# for whichever title someone picks, not just "python developer".
_KEYWORD_ROTATION = [
    "Software Engineer", "AI Engineer", "Data Scientist", "Machine Learning Engineer",
    "DevOps Engineer", "Cloud Engineer", "Product Manager", "Data Engineer",
    "QA Automation Engineer", "Site Reliability Engineer",
    "LLM Engineer", "GenAI Developer", "Backend Engineer", "Frontend Engineer",
    "Full Stack Developer", "Data Analyst", "Cybersecurity Engineer", "Cloud Architect",
    "Platform Engineer", "AI Research Engineer",
]
_rotation_index = {"i": 0}

# How many keywords to scrape CONCURRENTLY per scheduler tick. Each keyword
# fans out across all 10 portals internally already -- this multiplies that
# by running several keywords' worth of orchestrator calls in parallel
# threads instead of one keyword per tick, directly multiplying coverage
# per unit time without shortening the interval further.
_KEYWORDS_PER_CYCLE = 4


def _next_keywords(n: int) -> list:
    kws = []
    for _ in range(n):
        kws.append(_KEYWORD_ROTATION[_rotation_index["i"] % len(_KEYWORD_ROTATION)])
        _rotation_index["i"] += 1
    return kws


def scheduled_five_tier_job():
    """
    Cron task wrapper for 5-Tier Orchestrator pipeline. Dispatches
    _KEYWORDS_PER_CYCLE keywords CONCURRENTLY (threaded) each tick instead
    of one keyword per tick -- this is the main volume lever: same wall-clock
    interval, several times the scraped coverage per cycle.
    """
    import concurrent.futures
    keywords = _next_keywords(_KEYWORDS_PER_CYCLE)
    logger.info(f"[Scheduler] Cron job triggered: Scheduled 5-Tier Pipeline for {keywords} (parallel).")
    try:
        from pipeline.five_tier_orchestrator import run_five_tier_orchestrator

        def _run_one(kw):
            try:
                run_five_tier_orchestrator(keyword=kw, country="in", remote_only=False)
                logger.info(f"[Scheduler] Completed 5-Tier run for '{kw}'.")
            except Exception as ex:
                logger.error(f"[Scheduler] 5-Tier run failed for '{kw}': {ex}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=_KEYWORDS_PER_CYCLE) as executor:
            list(executor.map(_run_one, keywords))
    except Exception as e:
        logger.error(f"[Scheduler] 5-Tier scheduled pipeline execution error: {e}", exc_info=True)


def scheduled_max_coverage_job():
    """
    Scheduled cron callback for Max-Coverage 5-Method Ingestion Pipeline.
    Runs every 6 hours.
    """
    logger.info("[CronJob] Triggered scheduled Max-Coverage 5-Method Waterfall Ingestion...")
    try:
        from pipeline.max_coverage_orchestrator import run_max_coverage
        result = run_max_coverage(keyword="developer", country="in")
        logger.info(f"[CronJob] Scheduled Max-Coverage job completed: {result}")
    except Exception as exc:
        logger.error(f"[CronJob] Scheduled Max-Coverage job failed: {exc}")


# ---------------------------------------------------------------------------
# SPEC 2.6 -- 15-DAY RETENTION PURGE
#
# Runs once daily. Deletes Job rows whose EFFECTIVE AGE exceeds
# JOB_RETENTION_DAYS, where effective age uses the same precedence the search
# filters use: posted_date when known, fetched_at/scraped_at only as a
# fallback for rows with no posted_date. Keeping that precedence identical
# here matters -- purging on a different definition of "old" than the one the
# search uses would delete rows the UI still considered visible.
# ---------------------------------------------------------------------------
JOB_RETENTION_DAYS = 15
SEARCH_CACHE_RETENTION_DAYS = 15


def scheduled_retention_purge_job():
    """Deletes jobs older than JOB_RETENTION_DAYS and prunes the SearchCache ledger."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import or_, and_
    from DB import SessionLocal, Job
    from pipeline.search_cache import purge_orphaned_cache_rows

    logger.info(f"[Retention] Starting daily purge of jobs older than {JOB_RETENTION_DAYS} days...")
    db = SessionLocal()
    try:
        # Naive cutoff for the naive posted_date column, aware cutoff for the
        # tz-aware fetched_at/scraped_at columns -- comparing one naive value
        # against all three is silently fine on SQLite but raises on Postgres.
        cutoff_naive = _dt.utcnow() - _td(days=JOB_RETENTION_DAYS)
        cutoff_aware = _dt.now(_tz.utc) - _td(days=JOB_RETENTION_DAYS)

        stale_filter = or_(
            Job.posted_date < cutoff_naive,
            and_(
                Job.posted_date.is_(None),
                Job.fetched_at < cutoff_aware,
                or_(Job.scraped_at.is_(None), Job.scraped_at < cutoff_aware),
            ),
        )

        purged = db.query(Job).filter(stale_filter).delete(synchronize_session=False)
        db.commit()
        logger.info(f"[Retention] Purged {purged} job row(s) older than {JOB_RETENTION_DAYS} days.")

        # UNUSABLE-URL PURGE. Rows whose apply_url is a search-engine SERP
        # rather than a job posting can never be shown to a user -- the guard
        # rejects them on every read. They were being persisted by an old
        # `share_link` fallback in normalize.py (now fixed). This clears the
        # backlog so counts, dedup ratios and portal health stop being skewed
        # by rows that are structurally unusable.
        from sqlalchemy import or_ as _or
        junk = db.query(Job).filter(
            _or(
                Job.apply_url.like("%google.com/search%"),
                Job.apply_url.like("%ibp=htl;jobs%"),
                Job.apply_url.like("%bing.com/search%"),
                Job.apply_url.like("%google.com/url?%"),
            )
        ).delete(synchronize_session=False)
        db.commit()
        logger.info(f"[Retention] Purged {junk} row(s) with unusable search-engine apply URLs.")

        cache_purged = purge_orphaned_cache_rows(db, older_than_days=SEARCH_CACHE_RETENTION_DAYS)
        logger.info(f"[Retention] Purged {cache_purged} stale SearchCache ledger row(s).")

        return {"jobs_purged": int(purged or 0), "unusable_url_rows_purged": int(junk or 0),
                "cache_rows_purged": cache_purged}
    except Exception as e:
        db.rollback()
        logger.error(f"[Retention] Daily purge failed: {e}", exc_info=True)
        return {"jobs_purged": 0, "cache_rows_purged": 0, "error": str(e)}
    finally:
        db.close()


def start_scheduler(interval_hours: int = 12, five_tier_minutes: int = 45) -> BackgroundScheduler:
    """
    Initializes and starts the BackgroundScheduler.
    Schedules:
      1. Pipeline A (run_pipeline) every `interval_hours`
      2. 5-Tier Pipeline every 12 hours
      3. Max-Coverage 5-Method Waterfall every 6 hours
    """
    global scheduler

    if scheduler and scheduler.running:
        logger.info("[Scheduler] Scheduler is already running.")
        return scheduler

    scheduler = BackgroundScheduler(daemon=True)

    # 1. First scheduled job (Pipeline A)
    scheduler.add_job(
        scheduled_pipeline_job,
        trigger="interval",
        hours=interval_hours,
        id="scheduled_job_ingestion",
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 2. Second scheduled job for 5-Tier Pipeline (runs every 12 hours)
    scheduler.add_job(
        scheduled_five_tier_job,
        trigger="interval",
        minutes=five_tier_minutes,
        id="five_tier_pipeline_cron",
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 3. Third scheduled job for Max-Coverage 5-Method Waterfall (runs every 6 hours)
    scheduler.add_job(
        scheduled_max_coverage_job,
        trigger="interval",
        hours=6,
        id="max_coverage_pipeline_cron",
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 4. Daily 15-day retention purge (spec 2.6). Runs at 03:15 UTC -- a low
    #    traffic hour, and deliberately NOT on an interval trigger so it
    #    cannot re-fire repeatedly after a restart.
    scheduler.add_job(
        scheduled_retention_purge_job,
        trigger="cron",
        hour=3,
        minute=15,
        id="retention_purge_cron",
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        f"[Scheduler] APScheduler started successfully with 4 scheduled crons (Pipeline A every {interval_hours}h, "
        f"5-Tier every {five_tier_minutes}min, Max-Coverage every 6h, Retention purge daily 03:15 UTC)."
    )
    return scheduler


def stop_scheduler():
    """
    Stops the background scheduler cleanly.
    """
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] BackgroundScheduler stopped.")


def get_scheduler_status() -> Dict[str, Any]:
    """
    Returns current status of background scheduler and API quotas.
    """
    global scheduler
    is_running = bool(scheduler and scheduler.running)
    jobs_info = []

    if is_running:
        for job in scheduler.get_jobs():
            jobs_info.append({
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "max_instances": job.max_instances,
                "misfire_grace_time": job.misfire_grace_time,
            })

    return {
        "running": is_running,
        "jobs": jobs_info,
        "quota_tracker": quota_tracker.get_status(),
    }
