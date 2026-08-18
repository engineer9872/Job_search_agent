import sys
import os

# Ensure project root directory is in sys.path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("EnrichmentRunner")

from DB import SessionLocal, Job
from pipeline.enrich import RecruiterEnricher
from pipeline.normalize import NormalizedJob


def run_db_enrichment():
    session = SessionLocal()
    enricher = RecruiterEnricher()

    try:
        jobs = session.query(Job).all()
        total_jobs = len(jobs)
        logger.info(f"Loaded {total_jobs} job records from database for batch enrichment.")

        email_count = 0
        company_email_count = 0
        recruiter_name_count = 0
        enriched_jobs_count = 0

        for idx, job in enumerate(jobs, 1):
            # Create transient NormalizedJob for enrichment parser
            norm_job = NormalizedJob(
                title=job.title,
                company=job.company,
                city=job.city,
                country=job.country,
                source_platform=job.source_platform,
                apply_url=job.apply_url,
                description_snippet=job.description_snippet or "",
            )

            enricher.enrich_job(norm_job)

            # Update DB model fields
            has_enriched_field = False
            if norm_job.recruiter_email:
                job.recruiter_email = norm_job.recruiter_email
                email_count += 1
                has_enriched_field = True

            if norm_job.company_contact_email:
                job.company_contact_email = norm_job.company_contact_email
                company_email_count += 1
                has_enriched_field = True

            if norm_job.recruiter_name:
                job.recruiter_name = norm_job.recruiter_name
                recruiter_name_count += 1
                has_enriched_field = True

            if has_enriched_field:
                enriched_jobs_count += 1

        session.commit()
        logger.info("Database enrichment transaction committed successfully.")

        hit_rate = round((enriched_jobs_count / total_jobs * 100), 2) if total_jobs > 0 else 0.0

        print("\n" + "=" * 60)
        print("DATABASE RECRUITER ENRICHMENT REPORT")
        print("=" * 60)
        print(f"Total Jobs Processed in DB:       {total_jobs}")
        print(f"Jobs with recruiter_email:       {email_count}")
        print(f"Jobs with company_contact_email: {company_email_count}")
        print(f"Jobs with recruiter_name:        {recruiter_name_count}")
        print(f"Total Enriched Jobs:             {enriched_jobs_count}")
        print(f"Total Enrichment Hit Rate (%):    {hit_rate}%")
        print("=" * 60 + "\n")

    except Exception as e:
        logger.error(f"Enrichment error: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()


if __name__ == "__main__":
    run_db_enrichment()
