"""
Smart, filter-aware scrape cache (Spec Part 2).

PURPOSE
-------
Stop calling paid scraping providers (Apify / Firecrawl / SerpApi) on every
single user search. Instead:

  1. Build a deterministic cache key from the NORMALIZED filter combination.
  2. Look up SearchCache for that key.
  3. Apply the per-bucket refresh policy from pipeline/date_filters.py:
       - inside the serve window  -> serve straight from the Job table, ZERO
                                     paid API calls
       - outside the serve window -> trigger a scrape (FULL for 12h/24h,
                                     24h-DELTA for 7d/30d), then serve
  4. Record last_scraped_at so the next request can short-circuit.

The jobs themselves are NEVER stored here -- they stay in the `jobs` table
and are queried by their own fields exactly as before. This table is purely
a "have we scraped this combination recently enough" ledger.

IN-FLIGHT DE-DUPLICATION (Spec Part 3)
--------------------------------------
Two users hitting Search for the same filters at the same second used to
fire two identical (paid) scrapes. `ScrapeInFlightRegistry` makes the second
caller skip the scrape entirely and just read whatever the first one writes.
This is an in-process lock: correct for a single uvicorn worker, which is
how this app currently runs. See module note at the bottom for the
multi-worker caveat.
"""

import json
import time
import hashlib
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

from sqlalchemy.orm import Session

from DB import SearchCache
from pipeline.date_filters import (
    resolve_date_bucket,
    get_cache_policy,
    normalize_date_filter,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# 2.1 -- CACHE KEY DESIGN
# ===========================================================================

def _normalize_keyword_field(raw: Optional[str]) -> str:
    """
    Lowercase, collapse whitespace, split on commas, drop blanks, sort
    alphabetically, rejoin.

    Sorting is what guarantees that "python, react" and "React ,  Python"
    produce the SAME cache key -- term ORDER must never create a duplicate
    cache entry (and therefore a duplicate paid scrape).
    """
    if not raw or not isinstance(raw, str):
        return ""
    parts = []
    for chunk in raw.split(","):
        cleaned = " ".join(chunk.split()).strip().lower()
        if cleaned and cleaned != "all":
            parts.append(cleaned)
    return ",".join(sorted(set(parts)))


def _normalize_scalar(raw: Any, default: str = "all") -> str:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return "true" if raw else "false"
    s = str(raw).strip().lower()
    if not s:
        return default
    return s


def _canonical_job_type(raw: Any) -> str:
    """
    Collapse the many spellings of each job type onto ONE canonical token so
    that ?job_type=fulltime and ?job_type=full_time share a cache entry
    instead of triggering two identical scrapes.

    Kept intentionally identical to the canonicalization used by the SQL
    layer and the guard layer (see canonical_job_type in filter_guard.py).
    """
    s = _normalize_scalar(raw, "all")
    if s in ["all", "all types", "all job types", ""]:
        return "all"
    if s in ["contract", "contractor", "freelance", "contract-to-hire", "c2c"]:
        return "contract"
    if s in ["fulltime", "full-time", "full_time", "permanent"]:
        return "full_time"
    if s in ["parttime", "part-time", "part_time"]:
        return "part_time"
    if s in ["onsite", "onsite_only", "on-site"]:
        return "onsite_only"
    return s


def build_filter_dict(
    title: Optional[str] = None,
    q: Optional[str] = None,
    platform: Optional[str] = None,
    country: Optional[str] = None,
    remote_only: Any = False,
    job_type: Optional[str] = None,
    date_posted: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Produces the normalized filter dict that the cache key is hashed from.

    NOTE: `date_posted` is deliberately reduced to its BUCKET here, not kept
    as the raw string (spec 2.1/2.2). "past_7d" and "7d" must not be two
    separate cache entries -- they describe the same scrape.
    """
    # title and q are both "free text the user typed" as far as scraping is
    # concerned, so they are merged into one normalized keyword field. This
    # matches how api/routes/jobs.py builds the scrape keyword.
    merged_keywords = ",".join(
        [x for x in [_normalize_keyword_field(title), _normalize_keyword_field(q)] if x]
    )

    country_norm = _normalize_scalar(country, "all")
    if country_norm in ["", "all"]:
        country_norm = "all"

    platform_norm = _normalize_scalar(platform, "all")
    if platform_norm in ["", "all"]:
        platform_norm = "all"

    remote_bool = remote_only
    if isinstance(remote_only, str):
        remote_bool = remote_only.strip().lower() in ["true", "1", "yes"]

    return {
        "keywords": _normalize_keyword_field(merged_keywords),
        "platform": platform_norm,
        "country": country_norm,
        "remote_only": bool(remote_bool),
        "job_type": _canonical_job_type(job_type),
        "date_bucket": resolve_date_bucket(date_posted),
    }


def compute_cache_key(filter_dict: Dict[str, Any]) -> str:
    """sha256 over the sorted, JSON-serialized normalized filter dict."""
    serialized = json.dumps(filter_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ===========================================================================
# 2.4 -- REFRESH POLICY DECISION
# ===========================================================================

class CacheDecision:
    """
    Result of consulting the cache for one request.

    should_scrape : whether a live (paid) scrape must run for this request
    refresh_mode  : "full" | "delta" | None
    since_hours   : delta window in hours (None for a full scrape)
    reason        : human-readable why, surfaced in logs and API response
    """

    __slots__ = ("should_scrape", "refresh_mode", "since_hours", "reason",
                 "cache_key", "bucket", "last_scraped_at", "age_minutes")

    def __init__(self, should_scrape, refresh_mode, since_hours, reason,
                 cache_key, bucket, last_scraped_at=None, age_minutes=None):
        self.should_scrape = should_scrape
        self.refresh_mode = refresh_mode
        self.since_hours = since_hours
        self.reason = reason
        self.cache_key = cache_key
        self.bucket = bucket
        self.last_scraped_at = last_scraped_at
        self.age_minutes = age_minutes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cache_key": self.cache_key[:16],
            "date_bucket": self.bucket,
            "scrape_triggered": self.should_scrape,
            "refresh_mode": self.refresh_mode,
            "since_hours": self.since_hours,
            "reason": self.reason,
            "cache_age_minutes": (
                round(self.age_minutes, 1) if self.age_minutes is not None else None
            ),
        }

    def __repr__(self):
        return f"<CacheDecision scrape={self.should_scrape} mode={self.refresh_mode} reason={self.reason}>"


def evaluate_cache(
    db: Session,
    filter_dict: Dict[str, Any],
    date_posted: Optional[str],
) -> CacheDecision:
    """
    THE CORE LOGIC (spec 2.4). Decides, for this exact filter combination,
    whether we may serve from the Job table alone or must spend a scrape.
    """
    cache_key = compute_cache_key(filter_dict)
    policy = get_cache_policy(date_posted)
    bucket = policy["bucket"]
    serve_window = policy["serve_window_minutes"]
    refresh_mode = policy["refresh_mode"]
    delta_hours = policy["delta_hours"]

    row = db.query(SearchCache).filter(SearchCache.cache_key == cache_key).first()

    # ---- FIRST EVER REQUEST for this combination -> always a full scrape ---
    if row is None:
        return CacheDecision(
            should_scrape=True,
            refresh_mode="full",
            since_hours=None,
            reason="cache_miss_first_request",
            cache_key=cache_key,
            bucket=bucket,
        )

    now = datetime.utcnow()
    last = row.last_scraped_at
    if last is not None and last.tzinfo is not None:
        # Defensive: an older row (or another surface) may have written a
        # tz-aware value. Normalize to naive UTC so the subtraction below
        # can never raise on PostgreSQL.
        last = last.replace(tzinfo=None)

    if last is None:
        age_minutes = float("inf")
    else:
        age_minutes = (now - last).total_seconds() / 60.0

    # ---- INSIDE the serve window -> ZERO paid API calls --------------------
    if age_minutes < serve_window:
        return CacheDecision(
            should_scrape=False,
            refresh_mode=None,
            since_hours=None,
            reason=f"cache_hit_within_{serve_window}min_window",
            cache_key=cache_key,
            bucket=bucket,
            last_scraped_at=last,
            age_minutes=age_minutes,
        )

    # ---- STALE -> scrape, full or delta depending on bucket ----------------
    #  12h / 24h : full re-scrape (window is small, full scrape is cheap)
    #  7d  / 30d : delta scrape of only the last 24h. The older 6 (or 29) days
    #              are already in the Job table and are picked up for free by
    #              the existing "posted within past_Nd" SQL filter -- no merge
    #              logic needed beyond inserting the new rows.
    return CacheDecision(
        should_scrape=True,
        refresh_mode=refresh_mode,
        since_hours=delta_hours if refresh_mode == "delta" else None,
        reason=f"cache_stale_{round(age_minutes)}min_old_mode_{refresh_mode}",
        cache_key=cache_key,
        bucket=bucket,
        last_scraped_at=last,
        age_minutes=age_minutes,
    )


def record_cache_hit(db: Session, cache_key: str) -> None:
    """Bump hit_count so we can measure how many paid calls the cache saved."""
    try:
        row = db.query(SearchCache).filter(SearchCache.cache_key == cache_key).first()
        if row is not None:
            row.hit_count = (row.hit_count or 0) + 1
            db.commit()
    except Exception as e:
        db.rollback()
        logger.debug(f"[SearchCache] Failed to record cache hit: {e}")


def record_scrape(
    db: Session,
    cache_key: str,
    filter_dict: Dict[str, Any],
    bucket: str,
    refresh_mode: str,
) -> None:
    """
    Stamp last_scraped_at = now for this combination. Called AFTER a live
    scrape completes (or times out with work continuing in the background --
    see the note in api/routes/jobs.py about why we still stamp on timeout).
    """
    try:
        row = db.query(SearchCache).filter(SearchCache.cache_key == cache_key).first()
        now = datetime.utcnow()
        if row is None:
            row = SearchCache(
                cache_key=cache_key,
                filter_json=json.dumps(filter_dict, sort_keys=True),
                date_bucket=bucket,
                last_scraped_at=now,
                scrape_count=1,
                hit_count=0,
                last_refresh_mode=refresh_mode,
                created_at=now,
            )
            db.add(row)
        else:
            row.last_scraped_at = now
            row.date_bucket = bucket
            row.scrape_count = (row.scrape_count or 0) + 1
            row.last_refresh_mode = refresh_mode
            row.filter_json = json.dumps(filter_dict, sort_keys=True)
        db.commit()
        logger.info(
            f"[SearchCache] Recorded {refresh_mode} scrape for key={cache_key[:12]}... "
            f"bucket={bucket} at {now.isoformat()}"
        )
    except Exception as e:
        db.rollback()
        logger.warning(f"[SearchCache] Failed to record scrape for {cache_key[:12]}...: {e}")


# ===========================================================================
# PART 3 -- IN-FLIGHT SCRAPE DE-DUPLICATION
# ===========================================================================

class ScrapeInFlightRegistry:
    """
    Prevents two concurrent requests with the IDENTICAL cache key from each
    firing their own (paid) scrape.

    The first caller acquires the key and runs the scrape. Any caller that
    arrives while that scrape is still running is told to skip -- it simply
    reads whatever is in the Job table (including whatever the in-flight
    scrape has already committed) and returns.

    Entries are auto-expired after `stale_after_seconds` so a crashed or
    hung scrape can never permanently wedge a cache key.
    """

    def __init__(self, stale_after_seconds: int = 180):
        self._lock = threading.Lock()
        self._in_flight: Dict[str, float] = {}
        self.stale_after_seconds = stale_after_seconds

    def try_acquire(self, cache_key: str) -> bool:
        """True if this caller now owns the scrape; False if one is already running."""
        now = time.time()
        with self._lock:
            started = self._in_flight.get(cache_key)
            if started is not None and (now - started) < self.stale_after_seconds:
                return False
            self._in_flight[cache_key] = now
            return True

    def release(self, cache_key: str) -> None:
        with self._lock:
            self._in_flight.pop(cache_key, None)

    def active_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for t in self._in_flight.values()
                if (now - t) < self.stale_after_seconds
            )


# Process-wide singleton.
#
# CAVEAT (flagged for the user, not silently assumed away): this is an
# IN-PROCESS lock. It fully de-duplicates concurrent scrapes for a single
# uvicorn worker, which is how this app currently runs. If you later scale
# to `--workers N` or multiple containers, each process gets its own
# registry and you would need to move this to a DB row lock or Redis
# SETNX to keep the guarantee. The SearchCache table itself is shared and
# correct across processes -- only this concurrency guard is per-process.
scrape_registry = ScrapeInFlightRegistry()


# ===========================================================================
# 2.6 -- RETENTION HELPERS
# ===========================================================================

def purge_orphaned_cache_rows(db: Session, older_than_days: int = 15) -> int:
    """
    Deletes SearchCache ledger rows not touched in `older_than_days`. Low
    priority (they are tiny), but keeps the table from growing unbounded
    across thousands of one-off filter combinations.
    """
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    try:
        deleted = (
            db.query(SearchCache)
            .filter(SearchCache.last_scraped_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted or 0)
    except Exception as e:
        db.rollback()
        logger.warning(f"[SearchCache] Orphan purge failed: {e}")
        return 0


def get_cache_stats(db: Session) -> Dict[str, Any]:
    """Diagnostics for the /api/cache-stats endpoint."""
    try:
        rows: List[SearchCache] = db.query(SearchCache).all()
        total_scrapes = sum(int(r.scrape_count or 0) for r in rows)
        total_hits = sum(int(r.hit_count or 0) for r in rows)
        total_requests = total_scrapes + total_hits
        by_bucket: Dict[str, int] = {}
        for r in rows:
            by_bucket[r.date_bucket] = by_bucket.get(r.date_bucket, 0) + 1
        return {
            "cached_filter_combinations": len(rows),
            "total_live_scrapes": total_scrapes,
            "total_cache_hits": total_hits,
            "api_calls_avoided_pct": (
                round((total_hits / total_requests) * 100, 1) if total_requests else 0.0
            ),
            "combinations_by_bucket": by_bucket,
            "scrapes_in_flight": scrape_registry.active_count(),
        }
    except Exception as e:
        logger.warning(f"[SearchCache] Stats query failed: {e}")
        return {"error": str(e)}
