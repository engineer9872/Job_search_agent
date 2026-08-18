import sys
import os
import logging
from sqlalchemy import text

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from DB import SessionLocal, engine, Job, Base
from pipeline.normalize import match_canonical_title, normalize_job_type, extract_city_and_country

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_backfill():
    """
    Backfills and locks all database job records to strictly adhere to normalized enum fields:
    canonical_title, job_type, country, remote_flag.
    """
    logger.info("Initializing schema updates if needed...")
    
    # Ensure columns exist via DDL if SQLite needs alter table
    with engine.connect() as conn:
        inspector = conn.execute(text("PRAGMA table_info(jobs);")).fetchall()
        column_names = [row[1] for row in inspector]
        
        if "canonical_title" not in column_names:
            logger.info("Adding column 'canonical_title' to jobs table...")
            conn.execute(text("ALTER TABLE jobs ADD COLUMN canonical_title VARCHAR(100);"))
        if "skills" not in column_names:
            logger.info("Adding column 'skills' to jobs table...")
            conn.execute(text("ALTER TABLE jobs ADD COLUMN skills TEXT;"))
        conn.commit()

    db = SessionLocal()
    try:
        jobs = db.query(Job).all()
        logger.info(f"Loaded {len(jobs)} job records for normalization backfill.")

        updated_count = 0
        for job in jobs:
            # 1. Canonical Title
            c_title = match_canonical_title(job.title)
            job.canonical_title = c_title

            # 2. Job Type
            j_type = normalize_job_type(job.source_platform, job.job_type, job.title, job.description_snippet or "")
            job.job_type = j_type

            # 3. Country ISO Code
            _, iso_country = extract_city_and_country(job.city or job.country, default_country=job.country)
            if iso_country and len(iso_country) == 2 and iso_country.isalpha():
                job.country = iso_country.upper()
            elif iso_country in ["US", "IN", "GB", "CA", "AU", "DE"]:
                job.country = iso_country
            else:
                job.country = None

            # 4. Remote Flag
            job.remote_flag = bool(job.remote_flag)

            updated_count += 1
            if updated_count % 250 == 0:
                db.commit()
                logger.info(f"Backfilled {updated_count} records...")

        db.commit()
        logger.info(f"SUCCESS: Backfilled all {updated_count} database job records cleanly!")

    except Exception as e:
        db.rollback()
        logger.error(f"Backfill failed: {e}")
        raise e
    finally:
        db.close()


if __name__ == "__main__":
    run_backfill()
