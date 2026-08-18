import sys
import os
import logging
from typing import List, Tuple, Dict, Any

logger = logging.getLogger("JobSearchAgent.Pipeline")

from DB import init_db, SessionLocal, Job
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
    GoogleJobsConnector,
)
from pipeline.normalize import normalize_job_batch, NormalizedJob
from pipeline.dedup import Deduplicator, build_job_signature
from pipeline.enrich import RecruiterEnricher

LAST_PIPELINE_RUN: Dict[str, Any] = {
    "total_raw_fetched": 0,
    "total_normalized": 0,
    "duplicates_filtered": 0,
    "inserted_count": 0,
    "dedup_ratio_pct": 0.0,
    "clean_ratio_pct": 100.0,
}


def get_last_pipeline_metrics() -> Dict[str, Any]:
    """Returns metrics from the last pipeline run."""
    return LAST_PIPELINE_RUN.copy()


def get_existing_db_signatures(session) -> List[Tuple[str, str, str, str]]:
    """
    Retrieves existing job apply_urls plus raw (title, company, city) fields
    from database -- NOT flattened into one string, so dedup.py can gate
    on company and title separately instead of one blended fuzzy score.
    """
    existing_records = session.query(Job.apply_url, Job.title, Job.company, Job.city).all()
    signatures = []
    for apply_url, title, company, city in existing_records:
        signatures.append((apply_url, title or "", company or "", city or ""))
    return signatures


def fetch_from_source(
    source: str,
    country: str,
    keyword: str,
    company: str = None,
    pages: int = 1,
    where: str = None,
) -> Tuple[List[Any], str]:
    """
    Dispatches fetch call to requested connector source among the 10 active portals.
    """
    source_clean = source.lower().strip()
    raw_jobs = []

    try:
        if source_clean == "linkedin":
            connector = LinkedInJobsConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country, page=1, where=where)

        elif source_clean == "indeed":
            connector = IndeedConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country, page=1, where=where)

        elif source_clean == "glassdoor":
            connector = GlassdoorConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country, page=1, where=where)

        elif source_clean == "dice":
            connector = DiceConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean == "ziprecruiter":
            connector = ZipRecruiterConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean == "usajobs":
            connector = USAJobsConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean == "careerbuilder":
            connector = CareerBuilderConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean == "simplyhired":
            connector = SimplyHiredConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean == "weworkremotely":
            from connectors.rss_api import Layer1RSSAPIConnector
            raw_jobs = Layer1RSSAPIConnector()._fetch_rss_feed(
                "weworkremotely", "https://weworkremotely.com/remote-jobs.rss"
            )

        elif source_clean == "hired":
            connector = HiredConnector()
            raw_jobs = connector.fetch_jobs(keyword=keyword, country=country)

        elif source_clean in ["google_jobs", "google"]:
            connector = GoogleJobsConnector()
            for page in range(1, pages + 1):
                batch = connector.fetch_jobs(keyword=keyword, country=country, page=page, where=where)
                raw_jobs.extend(batch)

        else:
            logger.warning(f"Unknown source connector requested: '{source}'")

    except Exception as exc:
        logger.warning(f"Connector '{source_clean}' failed or unavailable for country '{country}': {exc}. Continuing with other sources.")

    return raw_jobs, source_clean


def run_pipeline(
    sources: List[str],
    country: Any,
    keyword: str,
    company: str = None,
    pages: int = 1,
    where: str = None,
    threshold: float = 88.0,
):
    """
    Executes Multi-Source Aggregation Pipeline across active portals:
    Fetch -> Normalize -> Dedup -> Upsert into Database.
    """
    if isinstance(country, str):
        country_list = [c.strip().lower() for c in country.split(",") if c.strip()]
    elif isinstance(country, list):
        country_list = [str(c).strip().lower() for c in country if str(c).strip()]
    else:
        country_list = ["us"]

    if not country_list:
        country_list = ["us"]

    logger.info("=" * 65)
    logger.info("Starting Multi-Source Job Aggregation Pipeline")
    logger.info(f"Sources: {sources} | Target Countries: {country_list} | Keyword: '{keyword}' | Company: '{company}'")
    logger.info("=" * 65)

    # 1. Initialize DB tables
    try:
        init_db()
        logger.info("Database schema initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

    db_session = SessionLocal()

    try:
        # 2. Fetch existing DB signatures
        existing_signatures = get_existing_db_signatures(db_session)
        logger.info(f"Loaded {len(existing_signatures)} existing job records from database for deduplication.")

        all_normalized_jobs: List[NormalizedJob] = []
        total_raw_count = 0

        # 3. Aggregate across requested countries and sources
        for c_code in country_list:
            for src in sources:
                logger.info(f"Processing source: '{src.upper()}' for country: '{c_code.upper()}'...")
                raw_jobs, platform = fetch_from_source(
                    source=src,
                    country=c_code,
                    keyword=keyword,
                    company=company,
                    pages=pages,
                    where=where,
                )
                total_raw_count += len(raw_jobs)
                logger.info(f"[{src.upper()} - {c_code.upper()}] Raw jobs fetched: {len(raw_jobs)}")

                # Normalize batch with specific country context
                normalized_batch = normalize_job_batch(
                    raw_jobs=raw_jobs,
                    source_platform=platform,
                    country=c_code,
                    company_name=company,
                )
                logger.info(f"[{src.upper()} - {c_code.upper()}] Normalized jobs: {len(normalized_batch)}")
                all_normalized_jobs.extend(normalized_batch)

        logger.info(f"Total Raw Jobs Collected across all sources/countries: {total_raw_count}")
        logger.info(f"Total Normalized Jobs ready for deduplication: {len(all_normalized_jobs)}")

        # 4. Deduplicate (fuzzy title matching + company + location + apply_url)
        deduplicator = Deduplicator(similarity_threshold=threshold)
        unique_jobs, duplicates_filtered = deduplicator.deduplicate(
            new_jobs=all_normalized_jobs,
            existing_signatures=existing_signatures,
        )

        logger.info(f"Deduplication complete. Kept {len(unique_jobs)} unique jobs. Filtered out {duplicates_filtered} duplicates.")

        # 5. Recruiters Enrichment
        enricher = RecruiterEnricher()
        unique_jobs = enricher.enrich_jobs_batch(unique_jobs)

        # 6. Database Upsert
        inserted_count = 0
        for norm_job in unique_jobs:
            try:
                db_job = Job(**norm_job.to_dict())
                db_session.add(db_job)
                db_session.commit()
                inserted_count += 1
            except Exception as e:
                db_session.rollback()
                logger.debug(f"Skipping duplicate/invalid insertion: {e}")

        logger.info(f"Successfully inserted {inserted_count} new job records into DB.")

        # Metrics calculation
        total_normalized = len(all_normalized_jobs)
        dedup_ratio = round((duplicates_filtered / total_normalized * 100), 1) if total_normalized > 0 else 0.0
        clean_ratio = round(100.0 - dedup_ratio, 1)

        LAST_PIPELINE_RUN.update({
            "total_raw_fetched": total_raw_count,
            "total_normalized": total_normalized,
            "duplicates_filtered": duplicates_filtered,
            "inserted_count": inserted_count,
            "dedup_ratio_pct": dedup_ratio,
            "clean_ratio_pct": clean_ratio,
        })

        logger.info(f"Pipeline Run Summary: {LAST_PIPELINE_RUN}")
        return {
            "status": "SUCCESS",
            "inserted_count": inserted_count,
            "duplicates_filtered": duplicates_filtered,
            "total_raw": total_raw_count,
            "metrics": LAST_PIPELINE_RUN,
        }

    except Exception as exc:
        logger.error(f"Pipeline execution error: {exc}", exc_info=True)
        db_session.rollback()
        return {"status": "ERROR", "message": str(exc)}
    finally:
        db_session.close()

