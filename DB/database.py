import os
import logging
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

SQLITE_FALLBACK_URL = "sqlite:///./jobs_dev.db"
DATABASE_URL = os.getenv("DATABASE_URL", SQLITE_FALLBACK_URL)


def _create_db_engine(db_url: str):
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    if db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        engine_kwargs = {"connect_args": connect_args, "pool_pre_ping": True}
    else:
        engine_kwargs = {
            "pool_pre_ping": True,
            "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
            "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
            "pool_recycle": 1800,
        }
    return create_engine(db_url, **engine_kwargs)


engine = _create_db_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency for obtaining a database session."""
    ensure_migrated()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_exists(conn, table: str) -> bool:
    from sqlalchemy import text as _text
    try:
        if conn.engine.dialect.name == "sqlite":
            r = conn.execute(_text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
            ), {"t": table}).fetchone()
        else:
            r = conn.execute(_text(
                "SELECT to_regclass(:t)"
            ), {"t": table}).fetchone()
            r = r if r and r[0] else None
        return bool(r)
    except Exception:
        return False


def _ensure_column(conn, table: str, column: str, ddl_type: str):
    """
    Minimal additive migration helper. This project has no Alembic setup, and
    Base.metadata.create_all() only CREATES tables -- it never ALTERs an
    existing one. Without this, a new column silently never appears on a
    database that already has the table, and every query referencing it fails
    at runtime rather than at startup.
    """
    from sqlalchemy import text as _text
    try:
        dialect = conn.engine.dialect.name
        if dialect == "sqlite":
            cols = [r[1] for r in conn.execute(_text(f"PRAGMA table_info({table})")).fetchall()]
        else:
            cols = [
                r[0] for r in conn.execute(_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"
                ), {"t": table}).fetchall()
            ]
        if not cols:
            return  # table doesn't exist yet; create_all will build it fully
        if column not in cols:
            conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            try:
                conn.commit()
            except Exception:
                pass
            logger.info(f"[Migration] Added column '{column}' to table '{table}'.")
    except Exception as e:
        logger.warning(f"[Migration] Could not ensure column '{table}.{column}': {e}")


def _backfill_posted_date_precision(conn):
    """
    Classifies pre-existing rows so the freshness filter has something to work
    with on day one. A stored posted_date at exactly 00:00:00 cannot have come
    from a real timestamp, so it is day-level at best; USAJOBS and
    WeWorkRemotely report true timestamps and are marked exact.
    """
    from sqlalchemy import text as _text
    try:
        # On a brand-new database the jobs table does not exist yet -- the
        # lazy ensure_migrated() at import time runs BEFORE create_all(). That
        # is a legitimate state, not an error, so return quietly instead of
        # logging a scary "no such table" warning on every fresh install.
        if not _table_exists(conn, "jobs"):
            return

        pending = conn.execute(_text(
            "SELECT COUNT(*) FROM jobs WHERE posted_date_precision IS NULL"
        )).scalar()
        if not pending:
            return

        conn.execute(_text(
            "UPDATE jobs SET posted_date_precision = 'unknown' "
            "WHERE posted_date_precision IS NULL AND posted_date IS NULL"
        ))
        conn.execute(_text(
            "UPDATE jobs SET posted_date_precision = 'day' "
            "WHERE posted_date_precision IS NULL "
            "AND CAST(posted_date AS VARCHAR) LIKE '% 00:00:00%'"
        ))
        conn.execute(_text(
            "UPDATE jobs SET posted_date_precision = 'exact' "
            "WHERE posted_date_precision IS NULL "
            "AND source_platform IN ('usajobs', 'weworkremotely')"
        ))
        conn.execute(_text(
            "UPDATE jobs SET posted_date_precision = 'day' "
            "WHERE posted_date_precision IS NULL"
        ))
        try:
            conn.commit()
        except Exception:
            pass
        logger.info(f"[Migration] Backfilled posted_date_precision for {pending} job row(s).")
    except Exception as e:
        logger.warning(f"[Migration] posted_date_precision backfill skipped: {e}")


_MIGRATIONS_APPLIED = False


def run_lightweight_migrations(force: bool = False):
    """
    Additive, idempotent schema fixes.

    Runs at startup via init_db(), but ALSO lazily on first DB access (see
    ensure_migrated() below). That second path matters: scripts, tests, agent
    tools and any worker that opens a session WITHOUT calling init_db() would
    otherwise hit `no such column` at query time on a database created before
    the column existed. Guarded by a module flag so the cost is one PRAGMA per
    process, not per query.
    """
    global _MIGRATIONS_APPLIED
    if _MIGRATIONS_APPLIED and not force:
        return
    try:
        with engine.connect() as conn:
            _ensure_column(conn, "jobs", "posted_date_precision", "VARCHAR(10)")
            _backfill_posted_date_precision(conn)
        _MIGRATIONS_APPLIED = True
    except Exception as e:
        logger.warning(f"[Migration] Lightweight migrations skipped: {e}")


def ensure_migrated():
    """Idempotent, safe to call from anywhere before touching the jobs table."""
    if not _MIGRATIONS_APPLIED:
        run_lightweight_migrations()


def init_db():
    """Initialize database tables with automatic fallback to local SQLite if PostgreSQL connection fails."""
    global engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        Base.metadata.create_all(bind=engine)
        run_lightweight_migrations()
        dialect_name = engine.dialect.name
        logger.info(f"Database schema initialized successfully using dialect: '{dialect_name}'.")
    except Exception as e:
        if not engine.url.drivername.startswith("sqlite"):
            logger.warning(
                f"Failed to connect to configured database ({engine.url.drivername}): {e}. "
                f"Falling back to local SQLite database ({SQLITE_FALLBACK_URL})."
            )
            engine = _create_db_engine(SQLITE_FALLBACK_URL)
            SessionLocal.configure(bind=engine)
            Base.metadata.create_all(bind=engine)
            run_lightweight_migrations()
            logger.info("Fallback SQLite database schema initialized successfully.")
        else:
            logger.error(f"Error initializing database schema: {e}")
            raise e

