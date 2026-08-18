import sys
import os
import logging
from sqlalchemy import text

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from DB import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_migration():
    logger.info("Initializing work_authorization_note column migration...")
    
    with engine.connect() as conn:
        dialect = engine.dialect.name
        logger.info(f"Using database dialect: {dialect}")
        
        if dialect == "sqlite":
            inspector = conn.execute(text("PRAGMA table_info(jobs);")).fetchall()
            column_names = [row[1] for row in inspector]
        else:
            # Postgres check
            query = text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='jobs';"
            )
            rows = conn.execute(query).fetchall()
            column_names = [row[0] for row in rows]
        
        if "work_authorization_note" not in column_names:
            logger.info("Adding column 'work_authorization_note' to jobs table...")
            conn.execute(text("ALTER TABLE jobs ADD COLUMN work_authorization_note TEXT;"))
            logger.info("Column added successfully.")
        else:
            logger.info("Column 'work_authorization_note' already exists in jobs table.")
            
        conn.commit()
    logger.info("Migration completed successfully.")


if __name__ == "__main__":
    run_migration()
