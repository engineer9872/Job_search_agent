import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PipelineDiagnostic")

from DB import init_db, SessionLocal, Job, RunLog
from connectors import (
    LinkedInJobsConnector,
    IndeedConnector,
    GlassdoorConnector,
    DiceConnector,
    ZipRecruiterConnector,
    USAJobsConnector,
    CareerBuilderConnector,
    SimplyHiredConnector,
    HiredConnector,
)
from connectors.rss_api import Layer1RSSAPIConnector
from pipeline.normalize import normalize_job_batch, parse_date
from pipeline.filter_guard import validate_direct_job_url, ThreeTierFilterGuard
from pipeline.filter_lock import create_locked_filter_spec

ACTIVE_PORTALS = [
    "linkedin", "indeed", "glassdoor", "dice", "ziprecruiter",
    "usajobs", "careerbuilder", "simplyhired", "weworkremotely", "hired"
]


def run_stage_instrumentation(portal_id: str, keyword: str = "developer", date_posted: str = "all") -> dict:
    """
    Part A — Pipeline Stage Instrumentation function.
    Tracks 6 explicit stages for portal_id:
      1. raw_fetched
      2. passed_normalization
      3. passed_url_validation
      4. passed_date_filter
      5. passed_guard_checks
      6. final_returned
    """
    logger.info(f"=== [STAGE INSTRUMENTATION] Portal: '{portal_id}' | DateFilter: '{date_posted}' ===")
    
    # STAGE 1: raw_fetched
    raw_jobs = []
    try:
        if portal_id == "linkedin":
            raw_jobs = LinkedInJobsConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "indeed":
            raw_jobs = IndeedConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "glassdoor":
            raw_jobs = GlassdoorConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "dice":
            raw_jobs = DiceConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "ziprecruiter":
            raw_jobs = ZipRecruiterConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "usajobs":
            raw_jobs = USAJobsConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "careerbuilder":
            raw_jobs = CareerBuilderConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "simplyhired":
            raw_jobs = SimplyHiredConnector().fetch_jobs(keyword=keyword, country="US")
        elif portal_id == "weworkremotely":
            raw_jobs = Layer1RSSAPIConnector()._fetch_rss_feed("weworkremotely", "https://weworkremotely.com/remote-jobs.rss")
        elif portal_id == "hired":
            raw_jobs = HiredConnector().fetch_jobs(keyword=keyword, country="US")
    except Exception as exc:
        logger.error(f"[{portal_id}] Source fetch exception: {exc}")

    stage_1_raw = len(raw_jobs)

    # STAGE 2: passed_normalization
    normalized = normalize_job_batch(raw_jobs, source_platform=portal_id, country="us")
    stage_2_norm = len(normalized)

    # STAGE 3: passed_url_validation
    stage_3_url_passed = []
    stage_3_rejected_urls = []
    for nj in normalized:
        jdict = nj.to_dict()
        jurl = jdict.get("url") or jdict.get("apply_url") or ""
        if validate_direct_job_url(portal_id, jurl):
            stage_3_url_passed.append(jdict)
        else:
            stage_3_rejected_urls.append(jurl)

    stage_3_count = len(stage_3_url_passed)

    # STAGE 4: passed_date_filter
    now = datetime.now(timezone.utc)
    stage_4_date_passed = []
    for jdict in stage_3_url_passed:
        posted_val = jdict.get("posted_date")
        if date_posted in ["past_24h", "24h", "today"]:
            posted_dt = parse_date(posted_val) if isinstance(posted_val, str) else posted_val
            if not posted_dt:
                fetched_val = jdict.get("fetched_at") or jdict.get("scraped_at")
                posted_dt = parse_date(fetched_val) if isinstance(fetched_val, str) else (fetched_val or now)
            if posted_dt:
                if posted_dt.tzinfo is None:
                    posted_dt = posted_dt.replace(tzinfo=timezone.utc)
                if posted_dt >= (now - timedelta(hours=24)):
                    stage_4_date_passed.append(jdict)
        else:
            stage_4_date_passed.append(jdict)

    stage_4_count = len(stage_4_date_passed)

    # STAGE 5: passed_guard_checks
    filter_spec = create_locked_filter_spec(
        job_title="all",
        platform=portal_id,
        country="US",
        remote_only=False,
        date_posted=date_posted,
        job_type="all",
    )
    guard = ThreeTierFilterGuard(filter_spec)
    stage_5_guard_passed = guard.process_guard_checks(stage_4_date_passed)
    stage_5_count = len(stage_5_guard_passed)

    # STAGE 6: final_returned
    stage_6_final = stage_5_count

    metrics = {
        "portal_id": portal_id,
        "date_filter": date_posted,
        "1_raw_fetched": stage_1_raw,
        "2_passed_normalization": stage_2_norm,
        "3_passed_url_validation": stage_3_count,
        "4_passed_date_filter": stage_4_count,
        "5_passed_guard_checks": stage_5_count,
        "6_final_returned": stage_6_final,
        "sample_rejected_urls": stage_3_rejected_urls[:5],
    }

    logger.info(
        f"[{portal_id.upper()}] raw={stage_1_raw} | norm={stage_2_norm} | "
        f"url_val={stage_3_count} | date_filt={stage_4_count} | "
        f"guard={stage_5_count} | final={stage_6_final}"
    )
    if stage_3_rejected_urls:
        logger.info(f"[{portal_id.upper()}] Sample rejected URLs: {stage_3_rejected_urls[:3]}")

    return metrics


def main():
    logger.info("Starting Part A Stage Instrumentation diagnostic across all 10 active portals...")
    results_all = {}
    results_24h = {}

    for p in ACTIVE_PORTALS:
        results_all[p] = run_stage_instrumentation(p, date_posted="all")

    for p in ACTIVE_PORTALS:
        results_24h[p] = run_stage_instrumentation(p, date_posted="past_24h")

    print("\n" + "=" * 90)
    print("STAGE INSTRUMENTATION RESULTS TABLE (date_posted='all')")
    print("=" * 90)
    print(f"{'Portal':<15} | {'Raw':<5} | {'Norm':<5} | {'URL Val':<7} | {'Date Filt':<9} | {'Guard':<5} | {'Final':<5}")
    print("-" * 90)
    for p, m in results_all.items():
        print(f"{p:<15} | {m['1_raw_fetched']:<5} | {m['2_passed_normalization']:<5} | {m['3_passed_url_validation']:<7} | {m['4_passed_date_filter']:<9} | {m['5_passed_guard_checks']:<5} | {m['6_final_returned']:<5}")

    print("\n" + "=" * 90)
    print("STAGE INSTRUMENTATION RESULTS TABLE (date_posted='past_24h')")
    print("=" * 90)
    print(f"{'Portal':<15} | {'Raw':<5} | {'Norm':<5} | {'URL Val':<7} | {'Date Filt':<9} | {'Guard':<5} | {'Final':<5}")
    print("-" * 90)
    for p, m in results_24h.items():
        print(f"{p:<15} | {m['1_raw_fetched']:<5} | {m['2_passed_normalization']:<5} | {m['3_passed_url_validation']:<7} | {m['4_passed_date_filter']:<9} | {m['5_passed_guard_checks']:<5} | {m['6_final_returned']:<5}")


if __name__ == "__main__":
    main()
