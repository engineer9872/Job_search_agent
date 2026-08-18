from DB.database import engine, SessionLocal, Base, get_db, init_db, run_lightweight_migrations, ensure_migrated
from DB.models import Job, RunLog, GuardAuditLog, SearchCache

__all__ = ["engine", "SessionLocal", "Base", "get_db", "init_db", "run_lightweight_migrations", "ensure_migrated", "Job", "RunLog", "GuardAuditLog", "SearchCache"]



# Apply additive migrations once per process, at import time.
#
# Without this, any entry point that opens a session WITHOUT first calling
# init_db() -- scripts/, tests/, agent tools, a worker process -- would raise
# `no such column: jobs.posted_date_precision` at query time on a database
# created before that column existed. The call is idempotent, guarded by a
# module flag, and costs a single PRAGMA per process.
try:
    ensure_migrated()
except Exception:  # never let a migration hiccup block importing the package
    pass
