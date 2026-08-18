#!/usr/bin/env python3
"""
DISABLED ON PURPOSE. DO NOT RE-ENABLE.

This script used to do:

    if not j.posted_date:
        j.posted_date = j.fetched_at or j.scraped_at or j.created_at or now_iso

That fabricates posting dates. `fetched_at` is when WE last re-confirmed the
listing, `scraped_at` is when WE first saw it, and `now_iso` is literally the
moment the script ran. None of them is when the employer posted the job.

Running it once stamps every undated row as freshly posted, so a listing that
has been up for 30 days starts reporting "4 hours ago" -- and then legitimately
passes a 24-hour filter, because the database now genuinely claims it is fresh.
Every layer downstream behaves correctly on corrupted input.

If you need to deal with rows that have no posting date, the correct tool is:

    python scripts/audit_laundered_dates.py          # report
    python scripts/audit_laundered_dates.py --fix    # reset to unknown

An unknown posting date is honest. A fabricated one is not, and it is
indistinguishable from a real one once written.
"""

import sys

MESSAGE = """
This script has been disabled because it fabricated posting dates.

It set posted_date to fetched_at / scraped_at / now for any row that had no
posting date. Those are OUR timestamps, not the employer's -- so every undated
job became "just posted", and 30-day-old listings started passing 24h filters.

What you probably want instead:

    python scripts/job_date_doctor.py            # diagnose everything
    python scripts/job_date_doctor.py --fix      # repair corrupted dates

Rows with no posting date are left as unknown. They still appear under
7-day and 30-day windows; they are excluded from 12h/24h, where an
unverifiable date cannot be trusted.
"""

if __name__ == "__main__":
    print(MESSAGE)
    sys.exit(1)
