"""
MAX-COVERAGE WATERFALL ORCHESTRATOR (Component 5)
Executes the 5-Method Ingestion Waterfall:
  Method 1: Direct-ATS Capture (Greenhouse, Lever, Workday, SmartRecruiters, Ashby)
  Method 5: Aggregator APIs (Adzuna, JSearch, Jooble, Arbeitnow, The Muse, Reed, Remotive)
  Method 4: Structured Data Harvesting (JSON-LD schema.org/JobPosting)
  Method 2: Apify Actors (Naukri, Indeed, LinkedIn, Glassdoor)
  Method 3: Self-Hosted Fallback Scrapers (Playwright / JSON-LD fallback)

Parallel execution via ThreadPoolExecutor.
Normalization -> Canonical ATS Merging -> Deduplication -> DB Insert -> Health Monitoring.
"""

import os
import sys
import time
import json
import logging
import concurrent.futures
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from DB import SessionLocal, Job, RunLog
from pipeline.method_health import MethodHealthMonitor
from pipeline.query_planner import QueryPlanner
from pipeline.normalize import normalize_job_batch, NormalizedJob
from pipeline.dedup import Deduplicator
from pipeline.runner import get_existing_db_signatures
from Scheduler.crons_jobs import quota_tracker

logger = logging.getLogger(__name__)


def run_max_coverage(
    keyword: str = "developer",
    country: str = "in",
    portals: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Executes the Max-Coverage 5-Method Waterfall Ingestion Pipeline.
    Returns structured result metrics.
    """
    start_time = time.time()
    health_monitor = MethodHealthMonitor()
    planner = QueryPlanner(health_monitor=health_monitor)

    plans = planner.plan_search(keyword=keyword, country=country, target_portals=portals)

    logger.info("=" * 70)
    logger.info(f"[Max-Coverage Waterfall] Starting run: keyword='{keyword}', country='{country}'")
    logger.info(f"[Max-Coverage Waterfall] Generated {len(plans)} execution plans across 5 methods.")
    logger.info("=" * 70)

    raw_candidates_by_method: Dict[str, List[Dict[str, Any]]] = {}
    method_metrics: Dict[str, Dict[str, Any]] = {}

    # Connector instantiations
    from connectors.greenhouse import GreenhouseConnector
    from connectors.lever import LeverConnector
    from connectors.workday import WorkdayConnector
    from connectors.smartrecruiters import SmartRecruitersConnector
    from connectors.ashby import AshbyConnector

    from connectors.adzuna import AdzunaConnector
    from connectors.jsearch import JSearchConnector
    from connectors.jooble import JoobleConnector
    from connectors.arbeitnow import ArbeitnowConnector
    from connectors.the_muse import TheMuseConnector
    from connectors.reed import ReedConnector
    from connectors.remotive import RemotiveConnector

    from connectors.naukri import NaukriConnector
    from connectors.indeed import IndeedConnector
    from connectors.linkedin import LinkedInJobsConnector
    from connectors.glassdoor import GlassdoorConnector

    from scrapers.jsonld_harvester import JSONLDHarvester
    from scrapers.custom_playwright import Layer3CustomScraper

    # Worker dispatch helpers
    def execute_method_1() -> List[Dict[str, Any]]:
        jobs = []
        try:
            jobs.extend(GreenhouseConnector().fetch_jobs())
        except Exception as e:
            logger.error(f"[Method 1] Greenhouse error: {e}")
        try:
            jobs.extend(LeverConnector().fetch_jobs())
        except Exception as e:
            logger.error(f"[Method 1] Lever error: {e}")
        try:
            jobs.extend(WorkdayConnector().fetch_jobs())
        except Exception as e:
            logger.error(f"[Method 1] Workday error: {e}")
        try:
            jobs.extend(SmartRecruitersConnector().fetch_jobs())
        except Exception as e:
            logger.error(f"[Method 1] SmartRecruiters error: {e}")
        try:
            jobs.extend(AshbyConnector().fetch_jobs())
        except Exception as e:
            logger.error(f"[Method 1] Ashby error: {e}")

        for j in jobs:
            j["_source_method"] = "Method 1 (Direct ATS)"
        return jobs

    def execute_method_5() -> List[Dict[str, Any]]:
        jobs = []
        if quota_tracker.can_fetch("adzuna"):
            try:
                adz = AdzunaConnector().fetch_jobs(country=country, keyword=keyword)
                jobs.extend(adz)
                quota_tracker.record_call("adzuna", 1)
            except Exception as e:
                logger.error(f"[Method 5] Adzuna error: {e}")

        if quota_tracker.can_fetch("jsearch"):
            try:
                js = JSearchConnector().fetch_jobs(keyword=keyword, country=country)
                jobs.extend(js)
                quota_tracker.record_call("jsearch", 1)
            except Exception as e:
                logger.error(f"[Method 5] JSearch error: {e}")

        try:
            jb = JoobleConnector(api_key=os.getenv("JOOBLE_API_KEY")).fetch_jobs(keyword=keyword, country=country)
            jobs.extend(jb)
        except Exception as e:
            logger.error(f"[Method 5] Jooble error: {e}")

        try:
            ab = ArbeitnowConnector().fetch_jobs(keyword=keyword, country=country)
            jobs.extend(ab)
        except Exception as e:
            logger.error(f"[Method 5] Arbeitnow error: {e}")

        try:
            tm = TheMuseConnector().fetch_jobs(keyword=keyword, country=country)
            jobs.extend(tm)
        except Exception as e:
            logger.error(f"[Method 5] TheMuse error: {e}")

        try:
            rd = ReedConnector(api_key=os.getenv("REED_API_KEY")).fetch_jobs(keyword=keyword, country=country)
            jobs.extend(rd)
        except Exception as e:
            logger.error(f"[Method 5] Reed error: {e}")

        try:
            rm = RemotiveConnector().fetch_jobs(keyword=keyword, country=country)
            jobs.extend(rm)
        except Exception as e:
            logger.error(f"[Method 5] Remotive error: {e}")

        for j in jobs:
            j["_source_method"] = "Method 5 (Aggregator APIs)"
        return jobs

    def execute_method_2() -> List[Dict[str, Any]]:
        jobs = []
        if quota_tracker.can_fetch("naukri"):
            try:
                nk = NaukriConnector().fetch_jobs(keyword=keyword, country=country)
                jobs.extend(nk)
                quota_tracker.record_call("naukri", 1)
                health_monitor.record_run("Method 2", "naukri", True, len(nk))
            except Exception as e:
                logger.error(f"[Method 2] Naukri error: {e}")
                health_monitor.record_run("Method 2", "naukri", False, 0, error_msg=str(e))

        if quota_tracker.can_fetch("indeed"):
            try:
                ind = IndeedConnector().fetch_jobs(keyword=keyword, country=country)
                jobs.extend(ind)
                quota_tracker.record_call("indeed", 1)
                health_monitor.record_run("Method 2", "indeed", True, len(ind))
            except Exception as e:
                logger.error(f"[Method 2] Indeed error: {e}")
                health_monitor.record_run("Method 2", "indeed", False, 0, error_msg=str(e))

        if quota_tracker.can_fetch("linkedin"):
            try:
                li = LinkedInJobsConnector().fetch_jobs(keyword=keyword, country=country)
                jobs.extend(li)
                quota_tracker.record_call("linkedin", 1)
                health_monitor.record_run("Method 2", "linkedin", True, len(li))
            except Exception as e:
                logger.error(f"[Method 2] LinkedIn error: {e}")
                health_monitor.record_run("Method 2", "linkedin", False, 0, error_msg=str(e))

        if quota_tracker.can_fetch("glassdoor"):
            try:
                gd = GlassdoorConnector().fetch_jobs(keyword=keyword, country=country)
                jobs.extend(gd)
                quota_tracker.record_call("glassdoor", 1)
                health_monitor.record_run("Method 2", "glassdoor", True, len(gd))
            except Exception as e:
                logger.error(f"[Method 2] Glassdoor error: {e}")
                health_monitor.record_run("Method 2", "glassdoor", False, 0, error_msg=str(e))

        for j in jobs:
            j["_source_method"] = "Method 2 (Apify Actors)"
        return jobs

    def execute_method_4() -> List[Dict[str, Any]]:
        jobs = []
        harvester = JSONLDHarvester()
        sample_urls = [
            f"https://www.linkedin.com/jobs/{keyword}-jobs",
            "https://remoteok.com",
        ]
        for target_url in sample_urls:
            try:
                j = harvester.fetch_url_jsonld(target_url, source_platform="jsonld")
                jobs.extend(j)
            except Exception as e:
                logger.debug(f"[Method 4] JSON-LD error for {target_url}: {e}")

        for j in jobs:
            j["_source_method"] = "Method 4 (Structured Data JSON-LD)"
        return jobs

    def execute_method_3() -> List[Dict[str, Any]]:
        jobs = []
        scraper = Layer3CustomScraper()
        for portal in ["internshala", "truelancer"]:
            try:
                res = scraper.fetch_portal_data({"id": portal, "name": portal.capitalize()}, keyword=keyword, country=country)
                jobs.extend(res)
            except Exception as e:
                logger.debug(f"[Method 3] Custom scraper error for {portal}: {e}")

        for j in jobs:
            j["_source_method"] = "Method 3 (Self-Hosted Fallback)"
        return jobs

    # Execute all planned methods concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_method = {
            executor.submit(execute_method_1): "Method 1",
            executor.submit(execute_method_5): "Method 5",
            executor.submit(execute_method_4): "Method 4",
            executor.submit(execute_method_2): "Method 2",
            executor.submit(execute_method_3): "Method 3",
        }

        for future in concurrent.futures.as_completed(future_to_method):
            m_name = future_to_method[future]
            try:
                res = future.result()
                raw_candidates_by_method[m_name] = res
                method_metrics[m_name] = {"raw_count": len(res), "status": "SUCCESS"}
            except Exception as exc:
                logger.error(f"[Max-Coverage Waterfall] {m_name} failed: {exc}")
                raw_candidates_by_method[m_name] = []
                method_metrics[m_name] = {"raw_count": 0, "status": f"FAILED: {exc}"}

    # Aggregate and Normalize across all methods
    all_normalized: List[NormalizedJob] = []
    total_raw = 0

    for m_name, raw_list in raw_candidates_by_method.items():
        total_raw += len(raw_list)
        if raw_list:
            # Normalize batch
            for raw in raw_list:
                src_platform = raw.get("source_platform") or raw.get("source_ats") or "aggregator"
                norm_batch = normalize_job_batch([raw], source_platform=src_platform, country=country)
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

        if unique_jobs:
            logger.info(f"[Max-Coverage Waterfall] Inserting {len(unique_jobs)} unique non-duplicate jobs into DB...")
            for norm_job in unique_jobs:
                try:
                    existing = db_session.query(Job).filter(Job.apply_url == norm_job.apply_url).first()
                    if not existing:
                        db_job = Job(**norm_job.to_dict())
                        db_session.add(db_job)
                        db_session.commit()
                        inserted_count += 1
                except Exception as insert_err:
                    db_session.rollback()

        # Record RunLogs
        for m_name, metrics in method_metrics.items():
            run_log = RunLog(
                portal=f"Waterfall ({m_name})",
                timestamp=datetime.now(timezone.utc),
                layer_used="Max-Coverage Waterfall",
                success=metrics["raw_count"] > 0,
                num_jobs_found=metrics["raw_count"],
                error_message=None if metrics["raw_count"] > 0 else metrics["status"],
            )
            db_session.add(run_log)
        db_session.commit()

    except Exception as err:
        logger.error(f"[Max-Coverage Waterfall] DB insertion error: {err}")
        db_session.rollback()
    finally:
        db_session.close()

    elapsed = round(time.time() - start_time, 2)
    logger.info(
        f"[Max-Coverage Waterfall] Finished in {elapsed}s: Raw={total_raw}, "
        f"Normalized={len(all_normalized)}, Duplicates={duplicates_count}, Inserted={inserted_count}."
    )

    return {
        "status": "SUCCESS",
        "elapsed_seconds": elapsed,
        "keyword": keyword,
        "country": country,
        "total_raw_fetched": total_raw,
        "total_normalized": len(all_normalized),
        "duplicates_filtered": duplicates_count,
        "inserted_count": inserted_count,
        "method_metrics": method_metrics,
    }
