#!/usr/bin/env python3
"""
FIND JOBS WHOSE posted_date WAS LAUNDERED BY A PORTAL BUMP.

Until this was fixed, every scrape overwrote a stored posted_date whenever the
portal reported a newer one. Job boards routinely bump or re-promote old
listings, and SerpApi/Google Jobs then reports the re-promotion date instead of
the original posting date. The result: a listing that had been up for 30 days
would end up stored as "4 hours ago" and legitimately pass a 24h filter.

The fix makes posted_date write-once going forward, but rows already corrupted
in your database stay corrupted. This script finds them.

THE TELL: `posted_date` is much NEWER than `scraped_at`. scraped_at is set once
when we first insert a row and is never touched again. So if we first saw a
listing 30 days ago, it cannot genuinely have been posted 4 hours ago -- that
date can only have arrived by overwrite.

    python scripts/audit_laundered_dates.py               # report only
    python scripts/audit_laundered_dates.py --fix         # reset them to unknown
    python scripts/audit_laundered_dates.py --fix --to-scraped   # or to first-seen

`--fix` sets posted_date back to NULL, which is honest: we no longer know when
it was posted. Those rows then show "Posting date not published" and are
excluded from 12h/24h windows, which is the correct treatment for a date we
cannot trust.
"""

import os
import sys
import argparse
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from DB import SessionLocal, Job, init_db  # noqa: E402


def _naive(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="Clear the untrustworthy posted_date (sets it to NULL).")
    ap.add_argument("--to-scraped", action="store_true",
                    help="With --fix, set posted_date to scraped_at instead of NULL.")
    ap.add_argument("--tolerance-hours", type=float, default=6.0,
                    help="How much newer than scraped_at counts as suspicious. Default 6.")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        rows = db.query(Job).filter(Job.posted_date.isnot(None)).all()
        suspects = []
        for j in rows:
            posted = _naive(j.posted_date)
            first_seen = _naive(j.scraped_at)
            if posted is None or first_seen is None:
                continue
            # Posted AFTER we first saw it (beyond tolerance) is impossible
            # unless the value was overwritten by a later scrape.
            if posted > first_seen + timedelta(hours=args.tolerance_hours):
                suspects.append((j, posted, first_seen))

        print("=" * 74)
        print("LAUNDERED posted_date AUDIT")
        print("=" * 74)
        print(f"  rows with a posted_date : {len(rows)}")
        print(f"  suspicious rows         : {len(suspects)}")
        if rows:
            print(f"  share of the table      : {len(suspects) / len(rows) * 100:.1f}%")

        by_platform = {}
        for j, _p, _f in suspects:
            by_platform[j.source_platform] = by_platform.get(j.source_platform, 0) + 1
        if by_platform:
            print("\n  by platform:")
            for k, v in sorted(by_platform.items(), key=lambda x: -x[1]):
                print(f"    {k:16s} {v}")

        print("\n  examples (posted_date claims to be newer than first sight):")
        for j, posted, first_seen in suspects[:10]:
            gap = (posted - first_seen).total_seconds() / 86400
            print(f"    {str(j.title)[:40]:42s} posted={posted:%Y-%m-%d} "
                  f"first_seen={first_seen:%Y-%m-%d}  (+{gap:.0f}d)")

        if not args.fix:
            print("\n  Report only. Re-run with --fix to clear these dates.")
            return

        for j, _p, f in suspects:
            if args.to_scraped:
                j.posted_date = f
                j.posted_date_precision = "day"
            else:
                j.posted_date = None
                j.posted_date_precision = "unknown"
        db.commit()
        target = "scraped_at (first sight)" if args.to_scraped else "NULL (unknown)"
        print(f"\n  FIXED: reset {len(suspects)} row(s) to {target}.")
        print("  Those rows now read 'Posting date not published' and are excluded")
        print("  from 12h/24h windows, which is correct for a date we cannot trust.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
