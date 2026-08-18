import os
import sys
import logging

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from DB import SessionLocal, Job
from pipeline.normalize import extract_city_and_country

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FixExistingLocations")


def fix_locations():
    """
    One-time migration script to clean up location data for existing job records in DB.
    Re-applies extract_city_and_country() to parse city and country fields cleanly.
    """
    db = SessionLocal()
    try:
        all_jobs = db.query(Job).all()
        total_rows = len(all_jobs)
        updated_rows = 0
        sample_changes = []

        logger.info(f"Starting location cleanup migration for {total_rows} existing job records...")

        for job in all_jobs:
            old_city = job.city
            old_country = job.country

            # Build location input string
            if old_city and old_country:
                loc_input = f"{old_city}, {old_country}"
            elif old_city:
                loc_input = old_city
            else:
                loc_input = old_country

            new_city, new_country = extract_city_and_country(loc_input, default_country=old_country)

            if new_city != old_city or new_country != old_country:
                job.city = new_city
                job.country = new_country
                updated_rows += 1

                if len(sample_changes) < 10:
                    sample_changes.append({
                        "id": job.id,
                        "title": job.title,
                        "old_city": old_city,
                        "old_country": old_country,
                        "new_city": new_city,
                        "new_country": new_country,
                    })

        db.commit()

        logger.info("=" * 65)
        logger.info("LOCATION MIGRATION COMPLETED SUCCESSFULLY")
        logger.info(f"  • Total Job Rows Processed: {total_rows}")
        logger.info(f"  • Total Job Rows Updated:   {updated_rows}")
        logger.info("=" * 65)

        if sample_changes:
            logger.info("Sample Before -> After Location Updates:")
            for idx, s in enumerate(sample_changes, 1):
                logger.info(
                    f"  {idx:2d}. Title: '{s['title'][:40]}'\n"
                    f"      Old: city='{s['old_city']}', country='{s['old_country']}'\n"
                    f"      New: city='{s['new_city']}', country='{s['new_country']}'"
                )

    except Exception as e:
        logger.error(f"Error during location migration: {e}", exc_info=True)
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    fix_locations()
