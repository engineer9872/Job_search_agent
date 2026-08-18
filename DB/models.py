import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Float,
    Boolean,
    Text,
    DateTime,
    Index,
    func,
)
from DB.database import Base


def generate_uuid():
    return str(uuid.uuid4())


class Job(Base):
    """
    SQLAlchemy model representing a normalized job listing.
    """
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    title = Column(String(255), nullable=False, index=True)
    canonical_title = Column(String(100), nullable=True, index=True)
    skills = Column(Text, nullable=True)
    company = Column(String(255), nullable=False, index=True)
    city = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True, index=True)
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    currency = Column(String(10), nullable=True)
    remote_flag = Column(Boolean, default=False, nullable=False, index=True)
    job_type = Column(String(50), nullable=True, index=True)
    source_platform = Column(String(50), nullable=False, index=True)
    apply_url = Column(Text, nullable=False, unique=True, index=True)
    description_snippet = Column(Text, nullable=True)
    posted_date = Column(DateTime, nullable=True, index=True)

    # How trustworthy posted_date actually is: exact | hour | day | week |
    # unknown. Recorded at normalization time, BEFORE the raw source text is
    # parsed away and its provenance is lost. pipeline/freshness.py grants a
    # rounding tolerance based on this, which is what lets a coarse "1 day
    # ago" job sit correctly inside a 24h window WITHOUT also letting a
    # "30+ days ago" job launder itself in via a fresh fetched_at.
    posted_date_precision = Column(String(10), nullable=True, index=True)

    fetched_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    recruiter_name = Column(String(255), nullable=True)
    recruiter_email = Column(String(255), nullable=True)
    company_contact_email = Column(String(255), nullable=True)
    raw_hash = Column(String(64), nullable=True, index=True)
    contract_type = Column(String(50), nullable=True, default="contract")
    work_authorization_note = Column(Text, nullable=True)
    scraped_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_jobs_title_company", "title", "company"),
        Index("idx_jobs_country_platform", "country", "source_platform"),
        Index("idx_jobs_raw_hash", "raw_hash"),
    )

    def __repr__(self):
        return f"<Job(id='{self.id}', title='{self.title}', company='{self.company}', platform='{self.source_platform}')>"


class RunLog(Base):
    """
    SQLAlchemy model tracking execution health per portal per run.
    """
    __tablename__ = "run_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    portal = Column(String(100), nullable=False, index=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    layer_used = Column(String(20), nullable=False)  # 'Layer 1', 'Layer 2', 'Layer 3', 'Layer 4'
    success = Column(Boolean, default=True, nullable=False)
    num_jobs_found = Column(Float, default=0, nullable=False)
    error_message = Column(Text, nullable=True)

    def __repr__(self):
        return f"<RunLog(portal='{self.portal}', layer='{self.layer_used}', success={self.success}, jobs={self.num_jobs_found})>"


class GuardAuditLog(Base):
    """
    SQLAlchemy model tracking Guard Check 3 arbitration decisions.
    """
    __tablename__ = "guard_audit_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    job_id = Column(String(36), nullable=False, index=True)
    filter_hash = Column(String(64), nullable=False, index=True)
    check_level = Column(String(20), nullable=False)  # 'Check 1', 'Check 2', 'Check 3'
    outcome = Column(String(20), nullable=False)     # 'INCLUDE', 'EXCLUDE'
    reason = Column(Text, nullable=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    def __repr__(self):
        return f"<GuardAuditLog(job_id='{self.job_id}', outcome='{self.outcome}', level='{self.check_level}')>"



class SearchCache(Base):
    """
    Filter-combination scrape LEDGER (Part 2.3).

    This table does NOT store jobs. Jobs stay in the `jobs` table exactly as
    before. This is purely a record of "have we already spent a live/paid
    scrape on this exact filter combination recently enough?" -- one row per
    unique normalized filter combination.

    TZ CONVENTION NOTE (audit finding, Part 1): this table deliberately uses
    NAIVE UTC datetimes throughout. The existing `jobs` table mixes both
    conventions -- `posted_date` is naive while `fetched_at`/`scraped_at` are
    DateTime(timezone=True). That mix is harmless on SQLite (its DATETIME
    storage format silently drops the offset) but raises
    "can't compare offset-naive and offset-aware datetimes" on PostgreSQL.
    New tables use naive UTC only so the arithmetic in
    pipeline/search_cache.py is dialect-independent.
    """
    __tablename__ = "search_cache"

    id = Column(String(36), primary_key=True, default=generate_uuid)

    # sha256 of the normalized filter combination (see pipeline/search_cache.py)
    cache_key = Column(String(64), nullable=False, unique=True, index=True)

    # Raw filter dict as JSON text -- for debugging/inspection only, never
    # used for matching (cache_key is the only lookup path).
    filter_json = Column(Text, nullable=True)

    # "12h" | "24h" | "7d" | "30d"
    date_bucket = Column(String(10), nullable=False, index=True)

    # When this combination was last actually scraped via a live API call.
    # NAIVE UTC (datetime.utcnow()).
    last_scraped_at = Column(DateTime, nullable=False, index=True)

    # Bookkeeping so we can see how much this cache is actually saving.
    scrape_count = Column(Float, default=0, nullable=False)
    hit_count = Column(Float, default=0, nullable=False)
    last_refresh_mode = Column(String(10), nullable=True)  # "full" | "delta"

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_search_cache_key_bucket", "cache_key", "date_bucket"),
    )

    def __repr__(self):
        return (
            f"<SearchCache(key='{self.cache_key[:12]}...', bucket='{self.date_bucket}', "
            f"last_scraped_at={self.last_scraped_at})>"
        )
