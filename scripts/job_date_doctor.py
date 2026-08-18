#!/usr/bin/env python3
"""
JOB DATE DOCTOR — one script, run it on your live database.

    python scripts/job_date_doctor.py            # diagnose only, changes nothing
    python scripts/job_date_doctor.py --fix      # repair, then re-verify

It answers one question definitively: is any job being shown under a date
filter it does not belong in, and if so, why.

WHY THIS EXISTS
---------------
"24 hour filter is showing a 30-day-old job" had a non-obvious cause. The
filter was correct. The stored posting dates were wrong, written days earlier,
by three separate mechanisms:

  1. BUMP LAUNDERING. The scrape upsert overwrote posted_date whenever a portal
     reported a newer one. Portals routinely re-promote old listings, and
     SerpApi/Google Jobs reports the re-promotion date. A 30-day-old listing
     became "4 hours ago" on its next scrape.

  2. FABRICATED BACKFILL. scripts/backfill_job_dates.py set posted_date to
     fetched_at / scraped_at / now for any row that had none. Those are OUR
     timestamps, not the employer's. Every undated job became "just posted".

  3. WRONG SOURCE FIELD. The Greenhouse normalizer read `updated_at`, which
     bumps on any edit, as if it were the posting date.

All three are fixed in code. This script finds and repairs rows that were
already corrupted before the fix, then proves the filters are clean.

THE DETECTION RULE
------------------
`scraped_at` is written once at INSERT and never touched again. So it is a hard
upper bound on how recently we could have learned about a listing. If
`posted_date` is meaningfully NEWER than `scraped_at`, that value cannot be
genuine -- we could not have seen a job before it existed. It arrived by
overwrite or fabrication.
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from DB import SessionLocal, Job, init_db  # noqa: E402
from pipeline.freshness import effective_freshness, is_fresh_enough  # noqa: E402
from pipeline.date_filters import resolve_cutoff_minutes, UI_DATE_FILTER_VALUES  # noqa: E402


GREEN = "PASS"
RED = "FAIL"


def naive(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def hr(title):
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def find_corrupted(db, tolerance_hours: float):
    """Rows whose posted_date is newer than the moment we first saw them."""
    out = []
    for j in db.query(Job).filter(Job.posted_date.isnot(None)).all():
        posted = naive(j.posted_date)
        first_seen = naive(j.scraped_at)
        if posted is None or first_seen is None:
            continue
        if posted > first_seen + timedelta(hours=tolerance_hours):
            out.append((j, posted, first_seen))
    return out


def find_suspicious_clusters(db):
    """
    A fabricated backfill leaves a fingerprint: a large number of rows whose
    posted_date lands in the same narrow moment (when the script ran), or
    equals fetched_at/scraped_at exactly.
    """
    exact_match_fetched = 0
    exact_match_scraped = 0
    buckets = {}
    for j in db.query(Job).filter(Job.posted_date.isnot(None)).all():
        p, f, s = naive(j.posted_date), naive(j.fetched_at), naive(j.scraped_at)
        if p and f and abs((p - f).total_seconds()) < 2:
            exact_match_fetched += 1
        if p and s and abs((p - s).total_seconds()) < 2:
            exact_match_scraped += 1
        if p:
            key = p.strftime("%Y-%m-%d %H:00")
            buckets[key] = buckets.get(key, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:3]
    return exact_match_fetched, exact_match_scraped, top


def verify_filters(db):
    """
    For every supported window, check every row that the date filter would
    admit and confirm its effective timestamp is genuinely inside the window.
    """
    now = datetime.utcnow()
    all_jobs = [
        {
            "title": j.title,
            "posted_date": j.posted_date,
            "posted_date_precision": j.posted_date_precision,
            "scraped_at": j.scraped_at,
            "fetched_at": j.fetched_at,
            "source_platform": j.source_platform,
        }
        for j in db.query(Job).all()
    ]

    results = {}
    for dp in UI_DATE_FILTER_VALUES:
        window = resolve_cutoff_minutes(dp)
        admitted, violations = [], []
        for d in all_jobs:
            ok, why = is_fresh_enough(d, window, now)
            if not ok:
                continue
            admitted.append(d)
            ts, _prec, src = effective_freshness(d)
            if ts is None:
                continue  # undated, already handled by is_fresh_enough
            age_h = (now - ts).total_seconds() / 3600
            if age_h > (window / 60) + 0.05:
                violations.append((d["title"], round(age_h, 1), src))
        results[dp] = (len(admitted), violations, window / 60)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="Repair corrupted posting dates (sets them to unknown).")
    ap.add_argument("--tolerance-hours", type=float, default=6.0,
                    help="How much newer than first-sight counts as impossible. Default 6.")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        total = db.query(Job).count()
        dated = db.query(Job).filter(Job.posted_date.isnot(None)).count()

        hr("1. DATABASE OVERVIEW")
        print(f"  total jobs                      : {total}")
        print(f"  with a stored posting date      : {dated}")
        print(f"  with no posting date            : {total - dated}")
        if total == 0:
            print("\n  Database is empty. Run a scrape first, then re-run this.")
            return

        hr("2. IMPOSSIBLE DATES  (posted_date newer than when we first saw it)")
        corrupted = find_corrupted(db, args.tolerance_hours)
        print(f"  corrupted rows                  : {len(corrupted)}"
              f"   ({len(corrupted) / max(dated, 1) * 100:.1f}% of dated rows)")
        if corrupted:
            by_platform = {}
            for j, _p, _f in corrupted:
                by_platform[j.source_platform] = by_platform.get(j.source_platform, 0) + 1
            print("\n  by platform:")
            for k, v in sorted(by_platform.items(), key=lambda x: -x[1]):
                print(f"    {str(k):16s} {v}")
            print("\n  examples:")
            for j, posted, first_seen in corrupted[:8]:
                gap = (posted - first_seen).total_seconds() / 86400
                print(f"    {str(j.title)[:44]:46s} claims {posted:%Y-%m-%d}, "
                      f"first seen {first_seen:%Y-%m-%d}  (+{gap:.0f}d impossible)")

        hr("3. FABRICATED-BACKFILL FINGERPRINT")
        eq_fetched, eq_scraped, top_buckets = find_suspicious_clusters(db)
        print(f"  posted_date identical to fetched_at : {eq_fetched}")
        print(f"  posted_date identical to scraped_at : {eq_scraped}")
        print("  busiest single hour of posting dates:")
        for k, v in top_buckets:
            flag = "  <-- suspicious pile-up" if v > max(20, dated * 0.05) else ""
            print(f"    {k}   {v} jobs{flag}")
        if eq_fetched > max(20, dated * 0.05):
            print("\n  A large block of rows has posted_date exactly equal to fetched_at.")
            print("  That is the signature of scripts/backfill_job_dates.py having been run.")
            print("  Those dates are fabricated. --fix will reset them to unknown.")

        if args.fix and corrupted:
            for j, _p, _f in corrupted:
                j.posted_date = None
                j.posted_date_precision = "unknown"
            db.commit()
            print(f"\n  FIXED: reset {len(corrupted)} impossible date(s) to unknown.")

        if args.fix and eq_fetched > max(20, dated * 0.05):
            n = 0
            for j in db.query(Job).filter(Job.posted_date.isnot(None)).all():
                p, f = naive(j.posted_date), naive(j.fetched_at)
                if p and f and abs((p - f).total_seconds()) < 2:
                    j.posted_date = None
                    j.posted_date_precision = "unknown"
                    n += 1
            db.commit()
            print(f"  FIXED: reset {n} fabricated backfill date(s) to unknown.")

        hr("4. FILTER VERIFICATION  (every row, every window)")
        results = verify_filters(db)
        total_viol = 0
        for dp, (admitted, violations, wh) in results.items():
            total_viol += len(violations)
            status = GREEN if not violations else RED
            print(f"  {dp:9s} window={wh:6.0f}h   returned={admitted:6d}   "
                  f"violations={len(violations):3d}   {status}")
            for v in violations[:3]:
                print(f"      LEAK: {v[1]}h old via {v[2]} -- {str(v[0])[:44]}")

        hr("VERDICT")
        clean = (total_viol == 0) and (len(corrupted) == 0 or args.fix)
        if total_viol == 0 and not corrupted:
            print("  PASS — no impossible dates, no filter violations.")
            print("  A 24h search cannot return a job older than 24h.")
        elif total_viol == 0 and corrupted and not args.fix:
            print(f"  ACTION NEEDED — {len(corrupted)} corrupted date(s) still stored.")
            print("  Filters are enforcing correctly, but they are enforcing against")
            print("  wrong data, so a stale job can still look fresh.")
            print("\n  Run:  python scripts/job_date_doctor.py --fix")
        elif total_viol == 0 and args.fix:
            print("  PASS — corrupted dates repaired and all filters verified clean.")
        else:
            print(f"  FAIL — {total_viol} filter violation(s) remain after repair.")
            print("  Send the section 4 output above; that names the field and age")
            print("  that got through.")
        print()
        return 0 if clean else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
