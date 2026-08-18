"""
Shared date-filter cutoff resolution + cache-bucket policy.

SINGLE SOURCE OF TRUTH for:
  1. What each date_posted value means, in minutes.
  2. Which cache bucket a date_posted value maps to.
  3. The refresh policy for each bucket (how long we serve from DB before
     re-scraping, and whether that re-scrape is a full or delta scrape).

Consumed by:
  - pipeline/filter_lock.py      (validates incoming values)
  - api/routes/jobs.py           (SQL query cutoff + cache decision)
  - pipeline/filter_guard.py     (post-scrape Step-5 date validation)
  - pipeline/search_cache.py     (cache key + refresh policy)
  - pipeline/capabilities.py     (freshness-precision decisions)

SUPPORTED FILTER SET (reduced per user request, 2026-08):
    past_12h | past_24h | past_7d | past_30d
Everything finer than 12h (10m/20m/30m/45m/1h/3h/6h) has been REMOVED --
none of the connectors have sub-hour freshness precision anyway (see
pipeline/capabilities.py: only USAJOBS and WeWorkRemotely report "exact"
timestamps), so those options were promising accuracy the data could not
deliver.

"all" / "all_time" is retained ONLY as a backward-compatible alias for
direct API callers and old bookmarks. It is no longer offered in the UI
and resolves to the 30-day hard cap -- never truly unlimited.
"""

from typing import Optional

MINUTE = 1
HOUR = 60
DAY = 24 * HOUR

# ---------------------------------------------------------------------------
# Canonical filter values -> cutoff window in minutes
# ---------------------------------------------------------------------------
DATE_FILTER_MINUTES = {
    # 12 hours
    "past_12h": 12 * HOUR,
    "12h": 12 * HOUR,
    # 24 hours
    "past_24h": 24 * HOUR,
    "24h": 24 * HOUR,
    "1d": 24 * HOUR,
    "today": 24 * HOUR,
    "past 24 hours": 24 * HOUR,
    # 7 days
    "past_7d": 7 * DAY,
    "7d": 7 * DAY,
    "7 days": 7 * DAY,
    "past week": 7 * DAY,
    # 30 days
    "past_30d": 30 * DAY,
    "30d": 30 * DAY,
    "30 days": 30 * DAY,
}

# Values the UI actually offers, in display order.
UI_DATE_FILTER_VALUES = ["past_12h", "past_24h", "past_7d", "past_30d"]

DEFAULT_CUTOFF_MINUTES = 7 * DAY
MAX_CUTOFF_MINUTES = 30 * DAY

LEGACY_ALL_VALUES = ["all", "all_time", "all time", ""]

# Windows tight enough that a stale DB row cannot be trusted to represent
# "posted within X" -- these get a shorter cache-serve window and a FULL
# re-scrape rather than a 24h delta scrape.
FRESHNESS_SENSITIVE_THRESHOLD_MINUTES = 24 * HOUR

VALID_DATE_FILTER_VALUES = list(DATE_FILTER_MINUTES.keys()) + LEGACY_ALL_VALUES


# ---------------------------------------------------------------------------
# SUPERSEDED: a single flat POSTED_DATE_GRACE_MINUTES constant used to live
# here. It has been replaced by the PRECISION-AWARE tolerance table in
# pipeline/freshness.py, because one flat grace value cannot be right for both
# a USAJOBS timestamp (needs 0 tolerance) and a "2 weeks ago" string (needs
# days of tolerance). All freshness arithmetic now lives in that one module,
# which both the SQL layer and the guard layer call.
# ---------------------------------------------------------------------------


def normalize_date_filter(date_posted: Optional[str]) -> str:
    """Lower/strip a raw date_posted value; empty -> '' (legacy 'all')."""
    if not date_posted or not isinstance(date_posted, str):
        return ""
    return date_posted.strip().lower()


def resolve_cutoff_minutes(date_posted: Optional[str]) -> int:
    """
    Returns the cutoff window in minutes for a given date_posted value.
    Never returns more than MAX_CUTOFF_MINUTES (30 days hard cap).
    """
    key = normalize_date_filter(date_posted)

    if not key:
        return DEFAULT_CUTOFF_MINUTES

    if key in LEGACY_ALL_VALUES:
        return MAX_CUTOFF_MINUTES

    minutes = DATE_FILTER_MINUTES.get(key)
    if minutes is None:
        # Unknown/removed value (e.g. an old "past_10m" bookmark) -> fall
        # back to the default window rather than erroring the request.
        return DEFAULT_CUTOFF_MINUTES

    return min(minutes, MAX_CUTOFF_MINUTES)


def resolve_date_bucket(date_posted: Optional[str]) -> str:
    """
    Maps a date_posted value onto its cache bucket: "12h" | "24h" | "7d" | "30d".
    The legacy "all" value shares the 30d bucket -- same window, same policy.
    """
    minutes = resolve_cutoff_minutes(date_posted)
    if minutes <= 12 * HOUR:
        return "12h"
    if minutes <= 24 * HOUR:
        return "24h"
    if minutes <= 7 * DAY:
        return "7d"
    return "30d"


def is_freshness_sensitive(date_posted: Optional[str]) -> bool:
    """
    True when the requested window is <= 24h -- tight enough that we do a
    FULL re-scrape on cache expiry instead of a 24h delta scrape.
    """
    return resolve_cutoff_minutes(date_posted) <= FRESHNESS_SENSITIVE_THRESHOLD_MINUTES


# ---------------------------------------------------------------------------
# CACHE REFRESH POLICY (Part 2.4)
#
#   serve_window_minutes -> how long after last_scraped_at we serve straight
#                           from the Job table with ZERO paid API calls.
#   refresh_mode         -> "full"  : re-scrape the whole window
#                           "delta" : scrape only the last `delta_hours`,
#                                     letting the existing SQL window
#                                     naturally include the older days
#                                     already sitting in the Job table.
#
# 24h -> 3h serve window + full re-scrape  (per spec)
# 7d / 30d -> 24h serve window + 24h delta scrape (per spec)
# 12h -> 90min serve window + full re-scrape. This bucket is NEW (it replaces
#        the removed sub-hour options); 90min keeps the same window:serve
#        ratio the spec set for 24h (1/8th of the window), and a full
#        re-scrape is correct here for the same reason it is at 24h -- the
#        window is small enough that a full scrape is cheap.
# ---------------------------------------------------------------------------
# `native_recency_hours` is what gets pushed DOWN to the portal as a
# source-side date filter (LinkedIn f_TPR, Indeed fromage+sort=date, SerpApi
# chips). This is separate from `delta_hours`:
#   delta_hours          = how much we RE-scrape on cache expiry
#   native_recency_hours = how we SCOPE the query at the source, every time
# The 12h/24h buckets do a FULL re-scrape, so delta_hours is None -- but they
# still want a tight source-side scope, otherwise the portal returns an
# unsorted default page and we throw almost all of it away post-fetch. That
# gap is exactly why tight windows came back nearly empty.
CACHE_POLICY = {
    "12h": {"serve_window_minutes": 90, "refresh_mode": "full",
            "delta_hours": None, "native_recency_hours": 24},
    "24h": {"serve_window_minutes": 180, "refresh_mode": "full",
            "delta_hours": None, "native_recency_hours": 24},
    "7d": {"serve_window_minutes": 1440, "refresh_mode": "delta",
           "delta_hours": 24, "native_recency_hours": 168},
    "30d": {"serve_window_minutes": 1440, "refresh_mode": "delta",
            "delta_hours": 24, "native_recency_hours": 720},
}


def get_cache_policy(date_posted: Optional[str]) -> dict:
    """Returns the CACHE_POLICY entry for this date_posted's bucket."""
    bucket = resolve_date_bucket(date_posted)
    policy = dict(CACHE_POLICY.get(bucket, CACHE_POLICY["7d"]))
    policy["bucket"] = bucket
    return policy
