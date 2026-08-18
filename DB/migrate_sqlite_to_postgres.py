import os
import sys
import logging
import sqlite3

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from DB import Job, Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MigrateSQLiteToPostgres")


def migrate():
    target_url = os.getenv("DATABASE_URL")
    if not target_url or target_url.startswith("sqlite"):
        logger.error("DATABASE_URL is not set to a PostgreSQL connection string in .env!")
        logger.info("Set DATABASE_URL=postgresql://user:pass@localhost:5432/dbname in .env and retry.")
        return

    sqlite_path = "jobs_dev.db"
    if not os.path.exists(sqlite_path):
        logger.error(f"Source SQLite database '{sqlite_path}' not found.")
        return

    logger.info(f"Target PostgreSQL URL: {target_url}")
    target_engine = create_engine(target_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=target_engine)
    TargetSession = sessionmaker(bind=target_engine)
    target_session = TargetSession()

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    try:
        rows = sqlite_cursor.execute("SELECT * FROM jobs").fetchall()
        logger.info(f"Found {len(rows)} job records in SQLite database to migrate.")

        migrated_count = 0
        for r in rows:
            row_dict = dict(r)
            # Check if record already exists in PostgreSQL
            existing = target_session.query(Job).filter(Job.id == row_dict["id"]).first()
            if not existing:
                job_obj = Job(**row_dict)
                target_session.add(job_obj)
                migrated_count += 1

        target_session.commit()
        logger.info(f"Successfully migrated {migrated_count} new job records to PostgreSQL!")

    except Exception as e:
        logger.error(f"Migration error: {e}", exc_info=True)
        target_session.rollback()
    finally:
        target_session.close()
        sqlite_conn.close()


if __name__ == "__main__":
    migrate()
