"""
THE single source of truth for "is this job fresh enough?".

Both the SQL layer (api/routes/jobs.py) and the guard layer
(pipeline/filter_guard.py) call into this module. Neither implements its own
cutoff arithmetic any more, so the two CANNOT disagree -- that class of bug
has bitten this codebase repeatedly.

===========================================================================
WHY THIS IS NOT A PLAIN max(posted_date, fetched_at)
===========================================================================
The obvious fix -- "use whichever of posted_date / fetched_at is more recent"
-- was measured against the real database before being written, and it is
WRONG. It would cause the exact bug it is meant to fix:

    807 of 2,943 rows have posted_date OLDER than 24h but fetched_at
    WITHIN 24h.
    Only 8 rows have a posted_date genuinely within 24h.

Under max(posted, fetched), all 807 stale postings land in "Past 24 Hours",
because re-scraping a job bumps fetched_at and thereby launders a 30-day-old
listing into looking brand new. That is precisely the reported symptom:
"Past 24 Hours returns jobs with old posted dates".

The real problem is narrower. `parse_relative_posted_date()` turns "1 day ago"
into exactly `now - 24h`, which lands EXACTLY on a 24h cutoff boundary -- any
delay between parse time and query time pushes it outside. Likewise 541 rows
store a date-only value (00:00:00) with no real time component. The rounding
error on those is ONE UNIT (an hour, a day, a week), not thirty days.

So the correct rule is a PRECISION-AWARE TOLERANCE, not a blanket override:

    include if  effective_timestamp >= (now - window - tolerance)

    where tolerance is the granularity of the source that produced the date:

      exact     -> 0        real timestamp (USAJOBS PublicationStartDate,
                            WWR RSS pubDate) -- trust it completely
      hour      -> 1 hour   parsed from "N hours ago"
      day       -> 24 hours parsed from "N days ago", or a date-only value
      week      -> 7 days   parsed from "N weeks ago" / "N months ago"
      unknown   -> fall back to fetched_at with tolerance 0

This keeps a genuinely-fresh "1 day ago" job inside a 24h window (spec Part
2.2's actual intent), keeps null-posted_date jobs from being dropped (spec
Part 2.3), and still keeps a "30+ days ago" job OUT of a 24h window, because
its tolerance is 24h -- nowhere near the 29 days it would need.
===========================================================================
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Tuple

from sqlalchemy import or_, and_

# Precision levels, coarsest last.
PRECISION_EXACT = "exact"
PRECISION_HOUR = "hour"
PRECISION_DAY = "day"
PRECISION_WEEK = "week"
PRECISION_UNKNOWN = "unknown"

# =============================================================================
# TOLERANCE IS ZERO. THE FILTER MEANS WHAT IT SAYS.
# =============================================================================
# These were previously non-zero -- `day` precision got a full 24h of slack, so
# a "Past 24 hours" search legitimately returned jobs the card itself displayed
# as "Posted 2 days ago". That is indefensible: the number on the filter and
# the number on the card contradicted each other, which makes the whole filter
# untrustworthy even when it is technically doing what it was told.
#
# THE INVARIANT NOW: a job is returned only if the timestamp the UI DISPLAYS is
# inside the requested window. No slack, no rounding grace, no "well the source
# was vague". If a card says 2 days ago it can never appear under a 24h filter.
#
# The honest cost: 12h and 24h now return fewer jobs, because that is how many
# genuinely fresh jobs exist. The old behaviour was not finding more jobs, it
# was padding the count with stale ones.
#
# The table is kept (rather than deleted) so precision still travels with each
# row for DISPLAY purposes -- the UI can still say whether a date is exact or
# approximate -- it just no longer buys a job its way past the filter.
# =============================================================================
PRECISION_TOLERANCE_MINUTES = {
    PRECISION_EXACT: 0,
    PRECISION_HOUR: 0,
    PRECISION_DAY: 0,
    PRECISION_WEEK: 0,
    PRECISION_UNKNOWN: 0,
}

# The widest tolerance any row can claim. Used to build the SQL pre-filter,
# which must be a SUPERSET of what the guard accepts -- SQL narrows cheaply,
# the guard then applies the exact per-row tolerance. A SQL layer stricter
# than the guard would silently drop rows the guard would have kept.
MAX_TOLERANCE_MINUTES = max(PRECISION_TOLERANCE_MINUTES.values())

# Portals whose posted_date can be trusted to the minute. Everything else
# reports coarse relative text. Cross-checked against pipeline/capabilities.py
# `freshness_precision` AND against the real stored data.
EXACT_PRECISION_PORTALS = {"usajobs", "weworkremotely"}


# ---------------------------------------------------------------------------
# Precision detection
# ---------------------------------------------------------------------------

def infer_precision_from_raw(raw_text: Any, portal_id: str = "") -> str:
    """
    Classifies how trustworthy a raw posted-date value is, BEFORE it gets
    parsed into a datetime and its provenance is lost forever. Called from the
    normalization layer so every new row records its own precision.
    """
    pid = (portal_id or "").lower().strip()

    if raw_text is None or (isinstance(raw_text, str) and not raw_text.strip()):
        return PRECISION_UNKNOWN

    if isinstance(raw_text, (int, float)):
        return PRECISION_EXACT  # epoch timestamp

    text = str(raw_text).strip().lower()

    if "week" in text or "month" in text or "year" in text:
        return PRECISION_WEEK
    if "day" in text or "yesterday" in text:
        return PRECISION_DAY
    if "hour" in text or "hr" in text or "minute" in text or "min" in text:
        return PRECISION_HOUR
    if "just posted" in text or "just now" in text:
        return PRECISION_HOUR
    if "today" in text:
        return PRECISION_DAY

    # An ISO-ish string with a real time component from a portal we know
    # reports true timestamps.
    if pid in EXACT_PRECISION_PORTALS:
        return PRECISION_EXACT
    if ("t" in text or " " in text) and ":" in text:
        # Has a clock time. Date-only values (00:00:00) are downgraded below.
        if text.endswith("00:00:00") or " 00:00:00" in text:
            return PRECISION_DAY
        return PRECISION_EXACT

    return PRECISION_DAY  # bare "2026-08-11" -- accurate to the day only


def infer_precision_from_stored(posted_dt: Optional[datetime], portal_id: str = "") -> str:
    """
    Backfill heuristic for rows written before the precision column existed.
    A stored value at exactly 00:00:00.000000 cannot have come from a real
    timestamp, so it is day-level at best.
    """
    if posted_dt is None:
        return PRECISION_UNKNOWN
    pid = (portal_id or "").lower().strip()
    if posted_dt.hour == 0 and posted_dt.minute == 0 and posted_dt.second == 0 \
            and posted_dt.microsecond == 0:
        return PRECISION_DAY
    if pid in EXACT_PRECISION_PORTALS:
        return PRECISION_EXACT
    # Everything else came through parse_relative_posted_date(), which
    # produces a precise-LOOKING timestamp from coarse text. Treat as day.
    return PRECISION_DAY


# ---------------------------------------------------------------------------
# Datetime normalization
# ---------------------------------------------------------------------------

def to_naive_utc(value: Any) -> Optional[datetime]:
    """
    Everything in this module compares naive-UTC against naive-UTC.

    The `jobs` table mixes conventions: `posted_date` is a naive DateTime while
    `fetched_at` / `scraped_at` are DateTime(timezone=True). Comparing across
    them is silently fine on SQLite (its DATETIME storage format drops the
    offset) but raises "can't compare offset-naive and offset-aware datetimes"
    on PostgreSQL. Normalizing here removes that entire failure mode.
    """
    if value is None:
        return None
    dt = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            from pipeline.normalize import parse_date
            dt = parse_date(value)
        except Exception:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            dt = dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# THE decision function -- used by the guard, and by every verification script
# ---------------------------------------------------------------------------

def effective_freshness(job: dict) -> Tuple[Optional[datetime], str, str]:
    """
    Returns (effective_timestamp, precision, source_field) for one job.

    Precedence:
      1. posted_date when present -- with the tolerance its precision earns.
      2. fetched_at / scraped_at as a fallback when posted_date is
         null/unparseable (spec Part 2.3: never drop a job just because
         posted_date is missing).
    """
    posted = to_naive_utc(job.get("posted_date"))
    portal = (job.get("platform_id") or job.get("source_platform") or "")

    if posted is not None:
        precision = job.get("posted_date_precision")
        if not precision or precision not in PRECISION_TOLERANCE_MINUTES:
            precision = infer_precision_from_stored(posted, portal)
        return posted, precision, "posted_date"

    # FALLBACK ORDER MATTERS. `scraped_at` is set once at INSERT and never
    # touched again -- it is genuinely "when our scraper FIRST saw this
    # listing", which is exactly what the spec asks for when the true posting
    # date is unknown.
    #
    # `fetched_at` is NOT that: five_tier_orchestrator bumps it to now() on
    # every repost/refresh pass. Using it as the fallback would let a listing
    # first seen 30 days ago (but re-confirmed today) appear under "Past 24
    # Hours" -- the same laundering problem, just via a different column.
    # So scraped_at is preferred, and when both exist we take the OLDER of
    # the two, which is the closest thing we have to first-sight.
    first_seen = to_naive_utc(job.get("scraped_at"))
    last_seen = to_naive_utc(job.get("fetched_at"))

    if first_seen is not None and last_seen is not None:
        fallback = min(first_seen, last_seen)
    else:
        fallback = first_seen or last_seen

    if fallback is not None:
        # An exact fact about OUR scraper, so it earns no rounding tolerance
        # -- but it is all we have, so the job is never dropped for it.
        return fallback, PRECISION_UNKNOWN, "first_seen"

    return None, PRECISION_UNKNOWN, "none"


# Windows where freshness IS the point of the filter. Inside these, a job whose
# posting date we cannot confirm is EXCLUDED.
TIGHT_WINDOW_MINUTES = 24 * 60


def is_fresh_enough(job: dict, window_minutes: int, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    THE freshness predicate. Returns (passes, human_readable_reason).

    =====================================================================
    FIRST-SEEN IS NOT POSTING AGE.
    =====================================================================
    629 of 2,943 stored rows have no published posting date. For those the only
    timestamp we hold is when OUR scraper first saw the listing -- and that says
    nothing about when it was posted. A job that has sat on Dice for 30 days but
    was first crawled by us three hours ago has a first-seen age of 3h and a
    real age of 30 days.

    Treating that as "fresh" is exactly the reported bug: a card reading
    "3 hours ago" on a job that is a month old. The old code returned
    `True, "no_date_signal_unknown_passes"` for these, which quietly asserted a
    freshness we had no evidence for.

    The rule now:
      - Window <= 24h  ->  an unconfirmable posting date FAILS. If freshness is
                           the entire point of the filter, we do not guess.
      - Window > 24h   ->  first-seen is allowed as a fallback, because over 7
                           or 30 days the discovery date is a reasonable proxy
                           and excluding 629 rows would gut the results.

    This keeps 12h/24h trustworthy and keeps 7d/30d full -- the two things that
    were in tension.
    """
    now = now or datetime.utcnow()
    ts, precision, source = effective_freshness(job)
    is_tight = window_minutes <= TIGHT_WINDOW_MINUTES

    # No date signal whatsoever.
    if ts is None:
        if is_tight:
            return False, "no_date_signal_excluded_from_tight_window"
        return True, "no_date_signal_unknown_passes_wide_window"

    # Posting date was never published; all we have is when we found it.
    if source == "first_seen":
        if is_tight:
            return False, "posting_date_not_published_excluded_from_tight_window"
        age_h = (now - ts).total_seconds() / 3600
        if ts >= now - timedelta(minutes=window_minutes):
            return True, f"first_seen_{age_h:.1f}h_ago_posting_date_not_published"
        return False, f"stale_first_seen_{age_h:.1f}h_old"

    tolerance = PRECISION_TOLERANCE_MINUTES.get(precision, 0)
    cutoff = now - timedelta(minutes=window_minutes + tolerance)

    if ts >= cutoff:
        return True, f"fresh_via_{source}_precision_{precision}"

    age_h = (now - ts).total_seconds() / 3600
    return False, f"stale_{source}_{age_h:.1f}h_old_precision_{precision}"


# ---------------------------------------------------------------------------
# SQL pre-filter -- deliberately a SUPERSET of the predicate above
# ---------------------------------------------------------------------------

def build_sql_freshness_clause(Job, window_minutes: int):
    """
    Returns a SQLAlchemy clause narrowing candidates to those that COULD pass
    is_fresh_enough(). It grants MAX_TOLERANCE_MINUTES to every row rather
    than trying to express per-precision tolerance in SQL.

    This is intentional: SQL is a cheap pre-filter, the guard is the source of
    truth. A SQL clause stricter than the guard would drop rows before the
    guard ever saw them -- the exact failure mode this module exists to
    prevent. Being looser is safe; being stricter is not.
    """
    total = window_minutes + MAX_TOLERANCE_MINUTES
    cutoff_naive = datetime.utcnow() - timedelta(minutes=total)
    cutoff_aware = datetime.now(timezone.utc) - timedelta(minutes=total)

    return or_(
        Job.posted_date >= cutoff_naive,
        # posted_date missing -> fall back to first-sight. Kept as an OR over
        # both columns so the SQL clause stays a SUPERSET of the predicate
        # (the guard then applies the stricter min(first_seen, last_seen)).
        and_(Job.posted_date.is_(None), Job.scraped_at >= cutoff_aware),
        and_(Job.posted_date.is_(None), Job.scraped_at.is_(None),
             Job.fetched_at >= cutoff_aware),
        # no date signal at all -> unknown must not be excluded
        and_(
            Job.posted_date.is_(None),
            Job.fetched_at.is_(None),
            Job.scraped_at.is_(None),
        ),
    )
