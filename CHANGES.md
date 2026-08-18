# TalentSphere AI — Caching, Filter Accuracy & UI Overhaul

Implementation of the 5-part spec. Everything below was verified against the
real `jobs_dev.db` (2,943 rows), not assumed.

---

## PART 1 — Filter audit: 6 real bugs found and fixed

Measured by running the OLD guard against the real DB before changing anything.

| # | Bug | Evidence | Fix |
|---|-----|----------|-----|
| 1 | `job_type=parttime` always returned zero. Frontend sends `parttime`, DB stores `part_time`. SQL mapped between them; the guard compared raw strings. | Old guard: 45/2943 passed — and all 45 had `job_type='unknown'`. **Not one real part-time job was ever returned.** | Single `canonical_job_type()` helper now used by SQL, guard and cache key. |
| 2 | `job_type=onsite` always returned zero. SQL treated it as `remote_flag == False`; the guard compared it as an employment-type string, which could never match. | Same 45/2943 — only unknown-type rows. | "Onsite" is now a **location** constraint on both layers. |
| 3 | Company/skill search broken. `q` was merged into the title term-groups, so the guard demanded the term appear in the job **title**, while SQL searched title/company/skills/description. | Searching `Amazon` returned 5 rows instead of every Amazon posting SQL matched. | `q` now travels in its own field and is validated across the same 5 fields SQL uses. `skills` added to `_serialize_job` so the guard can see it. |
| 4 | A 40-day-old posting satisfied a 7-day filter. The guard used `max(posted_date, fetched_at)`, so merely re-scraping a stale job made it look fresh. | — | `posted_date` is authoritative **when known**; `fetched_at` is a fallback only for rows with no `posted_date`. A shared `POSTED_DATE_GRACE_MINUTES = 60` absorbs relative-text rounding on **both** layers. |
| 5 | Would crash on PostgreSQL. A naive `utcnow()` cutoff was compared against the tz-aware `fetched_at`/`scraped_at` columns. Silently fine on SQLite; raises `can't compare offset-naive and offset-aware datetimes` on Postgres. | — | Naive cutoff for the naive `posted_date` column, aware cutoff for the aware columns. Applied in `/api/jobs` and `/api/status`. |
| 6 | `country=US` returned Germany-only remote roles — SQL OR'd in `remote_flag == True`. | — | A remote job satisfies a country filter only when its country is genuinely unknown or explicitly global. |

**Bonus bug:** the "Run Pipeline" button called `/api/pipeline/run`, which was
never implemented on the backend — it had been silently 404ing. Repointed to
`/api/pipeline/five-tier-run`.

**RULE 5 (missing ≠ conflict) is now strictly enforced.** NULL/`unknown`
`job_type`, NULL country and NULL date all PASS. Rejection happens only on a
confirmed conflict.

**Parity guarantee:** `_build_job_query()` in `api/routes/jobs.py` and the
`_check_*` methods in `pipeline/filter_guard.py` are written check-for-check
against each other, with cross-referencing comments. Both read their cutoffs
and constants from the single source of truth in `pipeline/date_filters.py`.

---

## PART 2 — Smart filter-aware caching (primary deliverable)

**New files:** `pipeline/search_cache.py`, `SearchCache` model in `DB/models.py`,
rewritten `pipeline/date_filters.py`.

Cache key = `sha256` of the normalized
`(keywords + platform + country + remote_only + job_type + date_bucket)`.
Keywords are lowercased, whitespace-collapsed, comma-split and
**alphabetically sorted**, so `"Python, React"` and `"react ,  PYTHON"` resolve
to the same key. `date_posted` is reduced to its **bucket**, so `past_7d` and
`7d` never become two entries.

`SearchCache` stores **no jobs** — it is purely a "have we already spent a paid
scrape on this combination" ledger. Jobs stay in the `jobs` table.

### Verified behaviour (live test against the real DB)

```
1st call                     total=77  scrape=True   cache_miss_first_request
2nd call (identical)         total=77  scrape=False  cache_hit_within_1440min   <- zero API calls
3rd call (term order swap)   total=77  scrape=False  cache_hit                  <- same key
remote_only=true             total=29  scrape=True   cache_miss                 <- independent entry
7d bucket, 30h staleness     total=77  scrape=True   mode=DELTA                 <- 24h delta only
24h bucket, 4h staleness     total=76  scrape=True   mode=FULL                  <- full re-scrape
```

### Bucket policy

| Bucket | Serve window | On expiry |
|---|---|---|
| `12h` | 90 min | FULL re-scrape |
| `24h` | 3 h | FULL re-scrape |
| `7d` | 24 h | DELTA — last 24h only |
| `30d` | 24 h | DELTA — last 24h only |

The 7d/30d delta relies on the existing `posted within past_Nd` SQL window to
pick up the older 6 (or 29) days already in the table — no merge logic beyond
inserting the new rows.

> **Judgment call, easy to override:** the `12h` bucket is new (not in the
> spec — it replaces the removed sub-hour options). 90 min keeps the same
> window-to-serve ratio the spec set for 24h (1/8th). Change it in
> `CACHE_POLICY` in `pipeline/date_filters.py`.

`run_five_tier_orchestrator()` gained `since_hours: Optional[int]`. Where a
source can express recency natively it is pushed down (SerpApi Google Jobs
`chips=date_posted:*`, RSS feeds which are recent-first by construction).
Where it cannot, the connector runs its normal query — the existing
normalize + dedup path still only ADDS genuinely new rows, so a delta run can
never duplicate what is already stored. **Existing dedup was reused, not
reimplemented.**

**Retention:** new daily APScheduler cron at 03:15 UTC purges jobs older than
15 days, using the *same* age definition the search filters use (purging on a
different definition would delete rows the UI still considered visible). Also
prunes stale `SearchCache` rows. Logs counts each run.

**New endpoint:** `GET /api/cache-stats` reports `api_calls_avoided_pct`,
combinations per bucket, and scrapes currently in flight.

---

## PART 3 — Scraping

### Implemented (low-risk)

**In-flight de-duplication** (`ScrapeInFlightRegistry`). Two concurrent
requests with an identical cache key no longer fire two paid scrapes — the
second reads whatever the first commits. Entries auto-expire after 180s so a
hung scrape can never permanently wedge a key.

> **Caveat, stated plainly:** this is an **in-process** lock. It is fully
> correct for a single uvicorn worker, which is how this app runs today. Under
> `--workers N` or multiple containers each process gets its own registry and
> you would need a Redis `SETNX` or DB row lock. The `SearchCache` table itself
> is shared and correct across processes — only this concurrency guard is not.

### Findings — NOT implemented, your call

1. **KeyRotator cooldown works — don't duplicate it.** The test run literally
   logged `All 5 configured FIRECRAWL keys are in cooldown from a recent
   failure`. **But:** the 300s cooldown is far too short for quota exhaustion.
   Quota-exceeded and transient failures should be treated differently — 1–6
   hours for quota, 300s for transient.

2. **`maxItems` caps are set blindly.** LinkedIn 300, everything else a flat
   200. **200 is nonsense for Hired** — `capabilities.py` itself marks it
   `low_yield_platform: true` with `freshness_precision: "unknown"`. 25–50 is
   realistic there. Indeed is already env-configurable via `INDEED_MAX_ITEMS`;
   the other connectors should follow that pattern rather than hardcoding.

3. **Portal contribution could not be honestly measured here.** Nine portals
   returned zero in the test run — but that is this sandbox's network policy
   (`serpapi.com` not in the egress allowlist, `apify_client` not installed),
   **not** a production signal. Check `visible_jobs_30d` per portal via
   `/api/status` in your live environment. Hired is structurally suspect (it is
   a candidate-matching platform, not a job board) but was **not** removed.

4. **No scrape frequency or volume was increased.** Scheduler still runs
   `_KEYWORDS_PER_CYCLE = 4` every 45 min, unchanged.

---

## PART 4 — Dead code removal (conservative)

**Removed — confirmed unused by full grep across backend and frontend:**
- 10 `.bak` / `.bak2` files
- `frontend/js/app.js` and `frontend/css/styles.css` — the entire legacy
  pre-React vanilla implementation. `index.html` loads only `/src/main.jsx`;
  no reference anywhere.

**Flagged, deliberately NOT removed:**
- `/api/pipeline/fallback-run` and `/api/pipeline/max-coverage-run` — no
  frontend caller, but may be invoked by scripts or curl.
- `Search/meilisearch_sync.py`, `alerts/webhook.py`, `enrich_db_jobs.py`,
  `DB/migrate_sqlite_to_postgres.py`, `DB/fix_existing_locations.py` — appear
  to be ops scripts.
- Sidebar has a hardcoded `"Esther Howard / Senior Full-Stack Candidate"`
  placeholder user. Cosmetic, but it will look odd in a demo.

Nothing was touched in `ThreeTierFilterGuard`, the 5-Tier Orchestrator, or the
new `SearchCache` system beyond the deliberate fixes above.

---

## PART 5 — UI / dashboard

**No new dependency.** `framer-motion` is not installed and was not added —
everything is plain CSS, so the bundle is unchanged (181 KB / 56.6 KB gzip).
The existing emerald/teal palette and Inter/Outfit typography from `:root` are
reused; nothing clashing was introduced.

- **Job cards** fade + lift in with a 30ms-per-card stagger, capped at 12 steps
  so a 50-card page settles in ~0.4s.
- **Skeleton loaders** mirror the real card layout, so the grid does not reflow
  when results land. This matters more now that a search may trigger a live
  scrape.
- **Hover/press states** on buttons, cards, dropdowns and nav items use
  elevation and transform shifts, not just colour.
- **Tab switching** cross-fades via `key={activeTab}` on a `.tab-panel` wrapper.
- **Empty state now diagnoses itself.** The API returns `empty_reason`, which
  distinguishes `no_candidates_in_db` (often just means a scrape was queued)
  from `all_candidates_filtered_out` (with the top 3 rejection reasons in plain
  English). This is precisely what was missing during earlier debugging, where
  a generic "No matching jobs found" was indistinguishable from a bug.
- **Live/Cached chip** in the results header shows scrape provenance.
- All durations sit in the 150–300ms band, and `prefers-reduced-motion` is
  honoured.

---

## Date filters — reduced to 4

`Past 12 Hours` / `Past 24 Hours` / `Past 7 Days` / `Past 30 Days`.
Removed: 10m, 20m, 30m, 45m, 1h, 3h, 6h, and "All Time".

The sub-hour options **could never have worked.** `capabilities.py` states that
only USAJOBS and WeWorkRemotely report `"exact"` timestamps — the other eight
portals are `"relative_text"` ("3 days ago"). Those options were promising
precision the data never had.

`date_posted` validation is now **lenient**: an old bookmark or a cached
frontend bundle still sending `past_10m` degrades to the default 7-day window
instead of returning a 400 and hard-failing the search.

`GET /api/date-filters` exposes the canonical list so frontend and backend can
never drift apart again.

---

## Verification performed

- 0 compile errors across the entire codebase
- FastAPI app boots cleanly (`from api.main import app`)
- Frontend builds successfully in 2.00s
- End-to-end search tested per bucket (12h / 24h / 7d / 30d), plus cache hit,
  cache expiry, delta vs full mode, and independent filter combinations

---

## Risk you should know about

The country and date filters are now **strictly tighter**. `country=US` no
longer surfaces a Germany-based remote role, and a 40-day-old posting no longer
appears under a 7-day filter. This is correct per the spec, but **your result
counts will drop on some searches** — that is the bug being fixed, not a
regression.

If you would rather remote jobs stay country-agnostic, it is one line in each
of two places: `_check_country` in `pipeline/filter_guard.py` and the country
block in `_build_job_query` in `api/routes/jobs.py`. Change both or they will
disagree again.

---

## Existing test suite — no regressions

Ran the repo's own `tests/` against the ORIGINAL code and the updated code and
diffed the failure lists:

```
BEFORE (original):  13 failed, 47 passed
AFTER  (updated):   13 failed, 48 passed
diff of FAILED lists: IDENTICAL
```

**All 13 failures pre-date these changes.** Notably
`tests/test_filter_guard.py` calls `check_1_exact_structural_match`,
`check_2_semantic_cross_validation` and `check_3_arbitration...` — methods that
did not exist in the original `filter_guard.py` either. That test file has been
stale for some time and is testing an architecture the code no longer has. It
was left alone rather than guessed at; worth rewriting against the current
`validate_job()` / `process_guard_checks()` API in a follow-up.

The one test that changed is `tests/test_filter_spec.py`, updated deliberately:
`date_posted` is now lenient by design (an unsupported value degrades to the
default window instead of returning a 400), so the assertion was rewritten to
cover that behaviour plus the four canonical values. `job_type` remains strict
and still raises.

---
---

# ROUND 2 — Freshness filters (24h/12h) & per-portal live fetch

## PART 1 — Diagnosis (measured against the real DB before any fix)

Four contributing causes, not one.

### Root cause A — the "obvious fix" is the bug

The spec asked for `max(posted_date, fetched_at)` as the effective freshness
timestamp. **Measured against the real 2,943-row database, that rule causes
the exact symptom being reported:**

```
Rows with posted_date OLDER than 24h but fetched_at WITHIN 24h : 807
Rows with posted_date genuinely within 24h                     :   8

max(posted, fetched) would admit into "Past 24 Hours" : 1118 rows
precision-aware rule admits (dated rows)              :   32 rows
```

`five_tier_orchestrator` bumps `fetched_at = now()` on every repost/refresh
pass. Under `max()`, re-scraping a listing **launders a 30-day-old posting
into looking brand new**. That is why "Past 24 Hours" was full of old jobs.

### Root cause B — coarse relative dates land exactly on the boundary

`connectors/serpapi_utils.parse_relative_posted_date()` converts `"1 day ago"`
to exactly `now - 24h`. That sits **precisely** on a 24h cutoff, so any delay
between parse time and query time flips it outside. Real spread in the DB:

| Portal | rows | null posted_date | date-only (00:00:00) |
|---|---|---|---|
| linkedin | 715 | 39 | **424** |
| usajobs | 296 | 16 | 117 |
| glassdoor | 313 | 68 | 0 |
| indeed | 282 | 115 | 0 |
| dice | 286 | 81 | 0 |
| ziprecruiter | 232 | 87 | 0 |
| simplyhired | 193 | 82 | 0 |
| careerbuilder | 159 | 84 | 0 |
| hired | 303 | 57 | 0 |
| weworkremotely | 164 | 0 | 0 |

**541 of 2,314 dated rows are stored at exactly 00:00:00** — no real time
component. Those cannot have come from a true timestamp.

### Root cause C — two layers, two implementations

Confirmed the SQL layer and the guard layer each did their own cutoff
arithmetic with their own constant. Fixed permanently (below).

### Root cause D — `past_12h` bucket

Verified: it **is** genuinely 720 minutes, distinct from 24h's 1440, and
correctly bucketed. Not a miscopy. Already present in the dropdown from
round 1. No bug here — reported as checked-and-clean.

---

## PART 2 — The fix: precision-aware freshness

**New module `pipeline/freshness.py` is now the SINGLE authority.** Both the
SQL layer and the guard layer call it; neither does its own arithmetic, so
they cannot drift apart again.

Instead of a blanket override, each row gets a **rounding tolerance equal to
the granularity of the source that produced its date**:

| precision | tolerance | source |
|---|---|---|
| `exact` | 0 | real timestamp (USAJOBS, WeWorkRemotely) |
| `hour` | 1 h | parsed from "N hours ago" |
| `day` | 24 h | "N days ago", or a date-only value |
| `week` | 7 d | "N weeks/months ago" |
| `unknown` | 0 | falls back to first-sight |

`include if effective_ts >= now - window - tolerance`

This keeps a genuine "1 day ago" job **inside** a 24h window (the spec's real
intent), and keeps a "30+ days ago" job **out** — its tolerance is 24h,
nowhere near the 29 days it would need.

**`scraped_at`, not `fetched_at`, is the null-posted_date fallback.**
`scraped_at` is written once at INSERT and never touched — genuinely "first
seen". `fetched_at` is bumped on every refresh, so using it would reintroduce
the same laundering via a different column. When both exist we take the older.

Jobs with **no date signal at all still PASS** — unknown is never a confirmed
conflict (spec Part 2.3).

**New DB column `posted_date_precision`** plus an additive, idempotent
migration in `DB/database.py` (this project has no Alembic, and
`create_all()` never ALTERs an existing table). Backfill classified all 2,943
existing rows: `day=1987, exact=327, unknown=629`.

### Verified — every returned job checked individually

```
past_12h  total=  36  jobs violating their own precision budget: 0  PASS
past_24h  total= 219  jobs violating their own precision budget: 0  PASS
past_7d   total= 871  jobs violating their own precision budget: 0  PASS
past_30d  total=1256  jobs violating their own precision budget: 0  PASS
```

---

## PART 3 — Coverage inside the window

### Scheduler adequacy — reported, NOT changed

Current: `_KEYWORDS_PER_CYCLE = 4` every 45 min over a 20-keyword rotation.
**A full rotation therefore takes ~3.75 hours.** For a 12h window that means
any given keyword is refreshed only ~3 times per window; for 24h, ~6 times.

That is thin but **defensible, and I did not touch it.** Given this project
has already exhausted SerpApi, Apify and Firecrawl credits simultaneously,
raising frequency is the wrong lever. The right lever is #2 and #3 below —
making each existing call return more genuinely-recent rows.

### Source-side recency scoping — implemented (this was the real gap)

Previously `since_hours` was passed **only on delta re-scrapes**. A 12h/24h
search does a FULL scrape, so it sent an **unsorted default query** and threw
away almost everything post-fetch. That is a large part of why tight windows
came back nearly empty.

Added `native_recency_hours` to `CACHE_POLICY`, applied on **every** scrape:

| bucket | serve window | refresh | source-side scope |
|---|---|---|---|
| 12h | 90 min | full | 24 h |
| 24h | 180 min | full | 24 h |
| 7d | 24 h | delta 24 h | 168 h |
| 30d | 24 h | delta 24 h | 720 h |

Wired into the connectors that have a native lever:
- **LinkedIn** — `f_TPR=r<seconds>`, `datePosted`, `sortBy=DD` (date desc)
- **Indeed** — `fromage=<days>`, `maxDaysOld`, `sort=date`
- **SerpApi Google Jobs** — `chips=date_posted:today|3days|week|month`

Connectors without a native lever are detected via signature introspection
and simply run their normal query — dedup still only ADDS new rows.

### maxItems — reported, NOT raised

Currently LinkedIn 300, everything else a flat 200. These are **not capped
too low**; if anything 200 is oversized for Hired (which `capabilities.py`
itself marks `low_yield_platform: true`). No increases made — that would raise
cost without raising the recent subset.

---

## PART 4 — Per-portal reliability

### The "Run Live Fetch" button was structurally broken

It POSTed to `/api/pipeline/five-tier-run`, which is a **fire-and-forget
BackgroundTask** returning `"ACCEPTED"` instantly. The UI then guessed an
8-second delay and refreshed — so the badge almost always showed a **stale**
status and the button looked like it did nothing.

**New endpoint `POST /api/portals/{portal_id}/live-fetch`:**
- **synchronous** — returns the real outcome of *this* fetch
- **scoped to one portal only**, validated against the portal config (404 otherwise)
- **uses the filters currently active in the search bar** (passed from `App.jsx`)
- **writes its own RunLog row before returning**, so the badge reflects this attempt
- respects the in-flight lock; bypasses the cache serve-window (explicit manual action)
- returns `raw_jobs_found`, `new_jobs_inserted`, `runtime_status`, and a plain-language message

Verified: badge flips on the fetched portal and **only** that portal.

### New endpoint `GET /api/portals/diagnostics`

Diagnoses each portal **individually** rather than assuming one shared cause,
and separates what is fixable in code from what needs your action:

`MISSING_CREDENTIALS` · `QUOTA_OR_CREDIT_EXHAUSTED` · `BLOCKED_OR_NETWORK_DENIED`
· `MISSING_PYTHON_DEPENDENCY` · `NEVER_RUN` · `ZERO_RESULTS_OR_CONNECTOR_ISSUE`
· `HEALTHY`

Returns a `needs_user_action` list — portals that no code change can fix.

---

## Latent bug this work exposed (and fixed)

Adding the new column broke 4 previously-passing tests with
`no such column: jobs.posted_date_precision`. The cause was **not** the
column — it was that scripts, tests and agent tools open DB sessions
**without ever calling `init_db()`**, so they never got migrations. That was a
live production hazard waiting for any schema change.

Fixed with an idempotent, flag-guarded `ensure_migrated()` invoked at
`DB/__init__` import and from `get_db()`. Cost: one PRAGMA per process.

**Final test result: 13 failed, 48 passed — byte-identical failure list to the
original untouched codebase. Zero regressions.**

---

## UI

- **Precision tag on every job card** — `exact` / `~day` / `first seen`, with
  tooltips. Given the backend now grants different tolerance per source,
  hiding this would mean showing "Posted 1 day ago" with identical confidence
  whether it came from a real USAJOBS timestamp or from scraped text that
  literally said "1 day ago".
- Job cards fall back to `scraped_at` for display when `posted_date` is null,
  instead of "Posting date unknown".
- Per-portal cards show the concrete outcome of the last manual fetch, so a
  zero-result run looks different from a failed one.
- Professional polish: tightened type rhythm (Outfit headings, tabular-nums on
  counts so they stop jittering), 4-step elevation scale, 3-line description
  clamp so one long card can't tower over its row, pill/badge restyling,
  visible focus rings for keyboard users, custom scrollbar.
- Still **no new npm dependency**. Bundle: 183 KB JS / 22.6 KB CSS.

---

## What needs YOUR action (cannot be fixed in code)

1. **Provider credits.** The test environment showed all 5 Firecrawl keys in
   cooldown, SerpApi returning 403, and `apify_client` not installed. Run
   `GET /api/portals/diagnostics` in your live environment — anything in
   `needs_user_action` needs keys or credits, not a patch.
2. **`pip install apify-client`** if the Apify fallback tiers are meant to work.
3. **Decide on scheduler frequency.** ~3.75h per full keyword rotation is thin
   for a 12h window. Options: shrink the rotation list to keywords you
   actually search, or raise `_KEYWORDS_PER_CYCLE`. Both cost API budget — I
   did not choose for you.

## Remaining risk

The `day`-precision tolerance is 24 hours. That means a **"Past 12 Hours"
search can legitimately return a job whose stored date reads up to 36 hours
old** — because the source only ever told us the day, not the hour. That is
honest rather than wrong (the alternative is silently dropping real jobs), and
the `~day` badge on the card makes it visible. If you want 12h to be
literally 12h, set `PRECISION_TOLERANCE_MINUTES["day"] = 0` in
`pipeline/freshness.py` — but expect that bucket to return close to zero,
since only 8 rows in the entire DB have a genuinely sub-24h exact timestamp.

---
---

# ROUND 3 — Live-fetch timing, Firecrawl exhaustion, dead portals, dedup

Every claim in the brief was verified against the real execution path first.
**Two of the four diagnoses were wrong**, and one flag turned out to be dead
code. Details below.

---

## PART 1 — The live-fetch diagnosis was backwards

**The brief said:** the endpoint returns stale data early, then the scrape
finishes later and the frontend never learns.

**What actually happens** (measured, not inferred):

```
timeout logged at t=2.0s
scrape finished at t=6.0s
with-block EXITED at t=6.0s   <-- when the request could actually respond
```

The code used `with concurrent.futures.ThreadPoolExecutor(...) as ex:`. The
context manager calls `shutdown(wait=True)` on exit, so the block **cannot**
return after the timeout — it blocks until the orchestrator finishes.

So the "12 second deadline" was **cosmetic**. Real behaviour: a 20-40 second
blocking request that logged `"Returning existing local results; the scrape
continues in the background"` halfway through — **a log line that was simply
false**. The response was late, not early. Anyone debugging from that log was
being actively misled.

### Fix

New `pipeline/scrape_jobs.py` with a **process-wide executor that outlives the
request** (not a per-request `with` block). A request waits up to
`FIRST_RESPONSE_DEADLINE_SECONDS = 20`, then genuinely returns while the
scrape keeps running.

- `/api/jobs` now returns `scrape_status` (`complete` | `in_progress` |
  `not_triggered`) and `scrape_poll_key`.
- New `GET /api/scrape-status/{cache_key}` — poll target.
- New `GET /api/scrape-status` — executor diagnostics.
- Frontend polls every 3s (capped at 40 attempts = 2 min), then **silently
  re-runs the same search** so late rows appear with no manual refresh. A live
  banner tells the user results are still arriving. The silent refresh
  deliberately does **not** flash the skeleton loader.
- The in-flight lock is now released **only when the scrape actually
  completed** — releasing early would let a concurrent request fire a
  duplicate paid scrape.

**Verified end to end:**
```
first response in 20.1s   total=6   scrape_status=in_progress
  poll 1: in_progress  20.0s
  poll 2: in_progress  23.1s
  poll 3: complete     25.1s  should_refresh=True
```

**Why 20s and not 35-40s as suggested:** 35-40s of dead air is worse UX than
20s + a self-updating list, and it still would not have covered the observed
worst case. Polling makes the deadline a responsiveness knob rather than a
correctness one.

---

## PART 2 — Firecrawl: one confirmed bug, one wrong claim

### CONFIRMED — `firecrawl_unsupported` was dead code

`pipeline/capabilities.py` has declared `"firecrawl_unsupported": True` for
LinkedIn since it was written. Grep result across the whole repo:

```
./pipeline/capabilities.py:37:        "firecrawl_unsupported": True,
```

**One hit — the declaration itself. Nothing ever read it.** So
`multi_source_race` submitted a Firecrawl branch for LinkedIn on every portal
fetch of every search, guaranteed to 403, burning a full 5-key rotation cycle
each time for zero possible benefit. This is the single biggest contributor to
the cooldown cascade in the log.

### WRONG — cooldown is NOT "all-or-nothing across all 5 keys"

`KeyRotator._cooldowns` is already a per-key dict with independent expiry
(`config/settings.py`). All five appear together in the log because all five
were tried and failed **in sequence within the same request** — coincident
timing, not a shared lock. Implementing "make cooldown per-key" would have
been a no-op. Also: a 2-minute log window cannot establish "never recovers"
when the cooldown is 300s. **No change made here — reporting it instead of
inventing a fix.**

### Fixes applied

1. **`firecrawl_supported()` / `playwright_supported()` in capabilities.py**
   — the flag is finally honoured. Checked in both `fetch_jobs_via_firecrawl()`
   and `multi_source_race`, so the branch is never even submitted.
2. **`FirecrawlCallBudget`** — hourly (40) and daily (300) ceilings, env-
   overridable via `FIRECRAWL_HOURLY_BUDGET` / `FIRECRAWL_DAILY_BUDGET`. Once
   spent, calls skip straight to the next tier with one log line instead of
   five doomed key checks per portal per request.
3. **Payload trimmed** — added `includeTags` (job-card elements only),
   `excludeTags` (nav/footer/script/style/svg/iframe/form/aside) and
   `removeBase64Images`. Cuts credits per call independently of caching.
4. **Cache gating already existed** from round 1 — `/api/jobs` consults
   SearchCache *before* any scrape, so a repeat search fires zero Firecrawl
   calls. Verified, not rebuilt.

**Measured:** a single search now consumes **5** Firecrawl calls instead of 8
— LinkedIn, ZipRecruiter and CareerBuilder are skipped entirely. Repeat
searches inside the cache window consume **0**.

---

## PART 3 — ZipRecruiter & CareerBuilder

Both are now marked `firecrawl_unsupported` + `skip_playwright` +
`primary_method_override: serpapi_google_jobs` in capabilities.py, so they go
**straight to SerpApi T4** instead of burning 15-20s on Firecrawl and
Playwright first. That brings them inside the 30s race window.

`portals_needing_api_key()` records what they actually need, and
`/api/portals/diagnostics` now reports them as **`NEEDS_PARTNER_API_KEY` /
`fixable_in_code: false`** regardless of what their last RunLog said —
because routing to SerpApi is a **latency workaround, not a substitute for the
real key**:

```
ziprecruiter   NEEDS_PARTNER_API_KEY   missing=['ZIPRECRUITER_PARTNER_API_KEY']
careerbuilder  NEEDS_PARTNER_API_KEY   missing=['CAREERBUILDER_API_KEY']
needs_user_action: ['ziprecruiter', 'careerbuilder']
```

**Playwright returning 0 — NOT fixed, and I will not guess.** Diagnosing a
broken selector versus a bot-detection block requires a headed browser run
against the live site, which cannot be done from this environment. Since both
portals now skip Playwright entirely, it is no longer on their critical path.
Flagged, not silently "fixed".

---

## PART 4 — Dedup was discarding fresh reposts

**Confirmed.** `Deduplicator.deduplicate()` matched on normalized
company+title+city and, on a fuzzy hit, did `duplicates_count += 1; continue`
— **no date comparison, and no record of which stored row it matched**, so
there was no way to refresh anything even if you wanted to.

### Fix

- Dedup now builds a `parts -> apply_url` map, so every fuzzy match is
  **attributable** to a specific stored row.
- Attributable matches go into `refresh_candidates` instead of being dropped.
- The orchestrator date-compares each candidate against the row it matched and
  **upserts `posted_date` + `fetched_at`** when the incoming copy is genuinely
  newer.
- `posted_date_precision` travels with the date — refreshing one without the
  other would let a coarse repost inherit an `exact` tolerance it never earned
  (see `pipeline/freshness.py`).
- **Skips and upserts are logged separately**, and the run result now returns
  `reposts_refreshed` and `duplicates_correctly_skipped`, so stale-vs-stale
  (correctly skipped) is finally distinguishable from stale-vs-fresh
  (previously lost).

---

## Verification

- 0 compile errors across the codebase
- FastAPI boots cleanly
- Frontend builds (184 KB JS / 23 KB CSS — still **no new npm dependency**)
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**
- Live search verified per part (output inline above)

---

## Needs YOUR action — cannot be fixed in code

1. **ZipRecruiter Partner API key** — apply at ziprecruiter.com/partner.
2. **CareerBuilder API key** — check developer.careerbuilder.com.
3. **Firecrawl credits.** All 5 keys were in cooldown throughout. The gating
   and budget above reduce burn substantially, but if the account itself is
   out of credit no code change brings it back.
4. **`pip install apify-client`** if the Apify fallback tiers are meant to run.
5. **Playwright 0-results** for ZipRecruiter/CareerBuilder — needs a headed
   browser run against the live sites to tell a broken selector from a bot
   block.

## Remaining risk

The 20s first-response deadline assumes the client polls. Any consumer of
`/api/jobs` that ignores `scrape_status` will still see a partial result set —
same as before, but now it is at least **labelled** rather than silent. If you
have other API consumers besides this dashboard, they need the same treatment.

---
---

# ROUND 4 — Free-first escalation ladder + exact-match ranking

## The plan, in one line

**Free techniques answer first. Paid APIs only run when the free tier failed to
answer the user's actual question — measured in EXACT MATCHES, not row count.**

```
Tier 0  FREE       native RSS/API -> JSON-LD -> static HTML      cost 0
          |  gate: enough EXACT matches? -- YES -> stop, spend nothing
          v  NO
Tier 1  SerpApi    ~1 call/portal, 1-3s, cheapest paid option
          |  gate: enough now? -- YES -> stop
          v  NO
Tier 2  Apify      heavy actor, 60-90s, ~$0.02/run. Only where a real actor exists
          |  gate: enough now? -- YES -> stop
          v  NO
Tier 3  Firecrawl  LAST. Budget-capped, skipped for portals that reject it
```

This **inverts** what was running. Previously Firecrawl raced on every request
unconditionally while SerpApi sat at tier 4 behind three broken tiers — the
most expensive provider ran first, the cheapest reliable one ran last.

---

## Why "exact match" and not row count

This is the core idea and it is what makes the ladder save money.

A "cloud engineer" search returning **40 rows of "Cloud Sales Executive" is a
failed search** that the old count-based logic treated as a success. **6 genuine
Cloud Engineer roles is a good search** the old logic would have escalated on.

New `pipeline/relevance.py` scores every job 0.0-1.0 against the parsed query:

| score | meaning |
|---|---|
| 1.00 | exact phrase in title |
| 0.85 | all query tokens in title, any order |
| 0.60 | majority tokens + a domain-synonym hit in title |
| 0.40 | all tokens, but only in company/skills/description |
| 0.15 | weak partial overlap |

Plus a **negative-signal guard**: a title containing `sales`, `recruiter`,
`marketing`, `intern` etc. is capped below the exact-match bar. Verified:

```
EXACT 1.00  Cloud Engineer                     [exact_phrase_in_title]
EXACT 1.00  Senior Cloud Engineer (AWS)        [exact_phrase_in_title]
      0.35  Cloud Sales Executive              [..._but_negative_title_signal]
EXACT 0.85  Cloud Infrastructure Engineer II   [all_tokens_in_title]
      0.40  Marketing Manager                  [all_tokens_but_only_in_body]
```

Escalation gate, verified:
```
6 exact cloud engineer   exact= 6  yield=1.00 -> FREE — stop
2 exact only             exact= 2  yield=1.00 -> SPEND MONEY
40 sales rows            exact= 0  yield=0.00 -> SPEND MONEY
15 exact                 exact=15  yield=1.00 -> FREE — stop
```

---

## Tier 0: the free tier (`pipeline/free_tier.py`)

Three techniques, cheapest first, per portal:

- **0a Native feed** — the portal's OWN free endpoint. USAJOBS official API
  (with `SortField=DateAdded`), WeWorkRemotely RSS, Dice RSS. Structured,
  **exact timestamps**, no key, no scraping.
- **0b JSON-LD** — `schema.org/JobPosting` embedded in the search page. Boards
  publish this so Google Jobs can index them; it is literally the same
  structured data SerpApi resells back to us.
- **0c Static HTML** — server-rendered job cards via BeautifulSoup.

### The bug that made the free tier return zero for every portal

The T3 dispatcher built its target URL as:

```python
f"https://{portal_id}.com"
```

`https://dice.com` — a **homepage**. No keyword, no search, no job cards. Every
portal's free tier was fetching a marketing landing page and correctly finding
nothing. **This was never bot detection.** Real URLs now, with native date
sorting where the board supports it:

```
dice           https://www.dice.com/jobs?q=cloud+engineer&location=...&filters.postedDate=ONE
indeed         https://www.indeed.com/jobs?q=cloud+engineer&l=...&sort=date&fromage=1
linkedin       https://www.linkedin.com/jobs/search?keywords=...&sortBy=DD&f_TPR=r86400
simplyhired    https://www.simplyhired.com/search?q=cloud+engineer&l=...&t=1
careerbuilder  https://www.careerbuilder.com/jobs?keywords=...&posted=1
```

Date-sorted URLs mean the free tier's **first page is the freshest page** —
exactly what the 12h/24h buckets need.

`JSONLDHarvester` already existed in `scrapers/jsonld_harvester.py` and was
wired into nothing on the live path. It is now connected — **that component was
built, not written from scratch.**

---

## Partial results are merged, never discarded

A portal that yields 3 free matches and 4 SerpApi matches returns **all 7**.
Free-tier jobs that were insufficient on their own are held in
`_FREE_TIER_PARTIAL` and merged into the paid result set, deduplicated by URL.
Even when every paid source fails, the free-tier partial is still returned
rather than nothing.

---

## Relevance ranking in the API

`/api/jobs` now ranks by relevance first, recency second, and returns
`match_quality`:

```json
{"query": "Cloud Engineer", "exact_matches": 62, "total": 72,
 "match_yield": 0.861, "avg_relevance": 0.792}
```

Each job carries `_relevance_score`, `_relevance_reason`, `_is_exact_match`, so
the UI can show **why** a result ranked where it did. Verified — top 8 results
for "Cloud Engineer" all scored 1.0:

```
EXACT 1.0  Principal Cloud Engineer
EXACT 1.0  Cloud Engineer (AWS & Terraform) - OKC
EXACT 1.0  Lead Azure Cloud Engineer@ 100% Remote Role
```

---

## Kill switch

`FREE_TIER_DISABLED=1` reverts to the old paid-first behaviour with no
redeploy, in case a portal starts serving something the free parser
misinterprets.

---

## Verification

- 0 compile errors · FastAPI boots · frontend builds (**no new npm dependency**)
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**
- Free tier degrades safely when a host is unreachable: returns
  `{blocked: 'http_403'}` and escalates rather than throwing.

---

## Honest limitation

**I could not measure the real free-tier hit rate from here.** This sandbox's
egress proxy returns 403 for every job-portal domain
(`x-deny-reason: host_not_allowed`), so `fetch_free_tier` correctly reports
`blocked` for all of them. That is my network, not the portals.

The ladder's *logic* is fully verified (URL construction, scoring, gating,
merging, degradation). What is NOT yet verified is **which portals actually
serve JSON-LD** — that needs `scripts/probe_portals.py` run from your machine.

Until then the ladder is safe but conservative: any portal whose free tier
comes back empty simply escalates to paid, which is exactly the old behaviour.
**Nothing gets worse; the upside is unlocked by running the probe.**

---
---

# ROUND 5 — Full flow audit + maximum jobs per request

## Filter matrix: every filter audited end to end

Ran a systematic matrix against the real 2,943-row DB. **All filters correct:**

| Check | Result |
|---|---|
| date_posted nesting (12h ⊆ 24h ⊆ 7d ⊆ 30d) | monotonic, no violation |
| platform (sum of 10 parts vs `all`) | SUM == all, exactly |
| job_type (all/fulltime/contract/parttime/onsite) | all non-zero, correctly narrowing |
| remote_only true vs unfiltered | true ⊆ unfiltered |
| country (US/IN/GB) | correct; unknown-country rows pass (RULE 5) |
| combined filters monotonically narrow | 1256 → 478 → 323 → 176, never widens |
| pagination | page1 ∩ page2 = ∅, no overlap, no gaps |
| limit delivery | 50/100/200 all deliver in full |
| nonexistent keyword | returns 0, not garbage |

---

## MAXIMUM JOBS — where jobs were actually being lost

Traced every row from DB to response:

```
DB total            2943
after SQL prefilter 2785
after guard         1936
LOST IN GUARD        849   <- invalid_or_indirect_url: 801, stale_date: 48
```

**801 rows were being discarded on every single read.** Two distinct causes,
both fixed.

### Cause 1 — garbage was being WRITTEN (fixed at the source)

`pipeline/normalize.py` fell back to `share_link` for SerpApi Google Jobs
items. For those items `share_link` is a **google.com/search?ibp=htl;jobs...**
URL — a Google results page, not a job posting.

```
usajobs  https://www.google.com/search?ibp=htl;jobs&q=software+engineer+usajobs...
hired    https://www.google.com/search?ibp=htl;jobs&q=software+engineer+hired...
dice     https://www.google.com/search?ibp=htl;jobs&q=software+engineer+dice...
```

These cost a scrape, occupied a row, inflated duplicate ratios, skewed portal
health — and could never be shown to anyone.

**Fixed:**
- New shared `is_usable_apply_url()` — one test used by every normalizer.
- Both normalizers now **walk every candidate** instead of taking
  `apply_options[0]` and giving up. A Google link at position 0 with a real
  Dice link at position 1 used to be lost; it is now recovered.
- The connector normalizer tries `url, link, apply_url, applyUrl, jobUrl,
  job_url, apply_link, detail_url` rather than just `url`/`link`.
- Unusable rows are **rejected at WRITE time**, not persisted then discarded.
- Daily cron purges the existing backlog (167 rows on the test DB).

Verified — previously-lost jobs now recovered:
```
input=3 kept=2
  Platform Engineer -> https://www.dice.com/job-detail/abc-123   (was lost: Google link was option[0])
  ML Engineer       -> https://www.dice.com/job-detail/xyz-9     (was lost: key was `jobUrl`)
```

### Cause 2 — the guard demanded the WRONG thing (the big one)

`validate_direct_job_url()` required the apply URL to be **on the portal's own
domain**. That is wrong. SerpApi and the portals themselves routinely return
apply links pointing at the employer's ATS or another board — all real,
applyable jobs:

```
hired        -> linkedin.com/jobs/view/flutter-developer-4449503404     REAL
usajobs      -> jobright.ai/jobs/info/6a7aa5b49ee17f276dbf3bd7          REAL
glassdoor    -> recruiterflow.com/talentsearchpro/jobs/5982             REAL
indeed       -> indeed.com/pagead/clk?...   (sponsored click)           REAL
ziprecruiter -> ziprecruiter.com/job-redirect?match_token=...           REAL
```

**~510 genuine, applyable jobs were being thrown away** for the crime of
living on a different domain than the portal they were filed under.

The correct test is **"is this ONE job posting, or a search/listing page?"** —
not "does the domain match". Rewrote accordingly:

- `_DIRECT_POSTING_PATTERNS` — accepts `/jobs/view/<id>`, `/jobs/info/<id>`,
  `?jk=`, `?gh_jid=`, `/viewjob`, ATS domains (Greenhouse, Lever, Workday,
  SmartRecruiters, RecruiterFlow, Ashby, Jobvite, iCIMS, Taleo), and apply
  redirects.
- `_SEARCH_PAGE_PATTERNS` — still rejects Google/Bing SERPs, `/jobs/search`,
  `/browse`, `/categories`, listing pages. **On any domain.**
- Added `pagead/clk` (Indeed) and `job-redirect` (ZipRecruiter) to their
  portal patterns.

Verified 8/8 on hand-built cases, including all five recoveries above plus
correct rejection of `linkedin.com/jobs/search/?keywords=...`,
`google.com/search?ibp=htl;jobs`, and `dice.com/jobs?q=engineer`.

---

## Result: jobs per request

| | before | after |
|---|---|---|
| Guard loss (unfiltered 30d) | **849** | **104** |
| Jobs served | 1,936 | **1,977** — from a DB 862 rows SMALLER |
| usajobs | 7 | **205** |
| hired | 36 | **187** |
| dice | 189 | **203** |
| "Software Engineer" | 558 | **588** |

**Guard loss down 88%.** The DB is *smaller* (junk purged) and yet serves
*more* jobs — because the rows it holds are now usable ones.

---

## Verification

- 0 compile errors · FastAPI boots · frontend builds (no new npm dependency)
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**
- Full filter matrix re-run after the fixes: every invariant still holds.

## Remaining 104 rejections

Genuinely unusable — bare search/listing pages with no posting ID. Correctly
rejected; showing them would send users to a search box instead of a job.

## Still needs your action

Unchanged from round 4: ZipRecruiter Partner API key, CareerBuilder API key,
Firecrawl credits, `pip install apify-client`, and running
`scripts/probe_portals.py` from your machine to unlock the free tier's real
hit rate.

---
---

# ROUND 6 — Visual system rebuild + Firecrawl single-key

## Firecrawl: one key, by design

Multi-key rotation was **actively harmful** here, not merely unnecessary.

Firecrawl bills credits against **one account**, so five keys never bought five
times the quota. What they bought was five times the failure logging: every
portal fetch walked all five keys, and when the account ran dry all five failed
in sequence *within a single request* and landed in cooldown together. From
then on every portal fetch of every search emitted five
`Key #N is in cooldown, skipping` lines before giving up — pure latency and log
noise with zero chance of success.

**Changes:**
- `KeyRotator` gained `single_key_only=True`, used for Firecrawl. In this mode
  the **bare** `FIRECRAWL_API_KEY` is authoritative, so a leftover
  `FIRECRAWL_API_KEY_1..N` in an old `.env` is ignored rather than silently
  taking precedence.
- If extra keys are found they are dropped with one clear log line, not five.
- `.env` and `.env.example` collapsed to a single `FIRECRAWL_API_KEY`.
- Verified: `keys: 1  single_mode: True`.

The hourly/daily budget in `connectors/firecrawl_client.py` (40/300,
env-overridable) plus the portal-support gate remain the real spend controls.

---

## Visual system: rebuilt from scratch

The old look was the generic SaaS default — emerald/teal on white, Outfit +
Inter, gradient-ish stat cards. Replaced with a system derived from what this
product actually is.

**Grounding.** This is a job-hunt terminal for someone actively searching who
opens it several times a day. Its one job: make scanning fast and make **match
quality legible at a glance**. The backend's real differentiator is exact-match
scoring, so the interface is built around reading signal out of noise —
departure-board / trading-terminal vernacular, not marketing polish.

### Palette (deliberately not the defaults)

| token | hex | role |
|---|---|---|
| ink | `#16181D` | text, sidebar rail |
| paper | `#F6F6F3` | workspace ground |
| surface | `#FFFFFF` | cards |
| signal | `#2B4EFF` | the single action accent — electric ultramarine |
| match | `#0A7A4A` | **semantic only** — an exact match |
| approx | `#A06A00` | **semantic only** — an approximate date |
| rule | `#E2E2DC` | hairlines |

Green and amber are never decorative here. They mean one thing each, and that
meaning comes from the backend.

### Type

- **Instrument Sans** — UI and display (not Inter, not Outfit)
- **IBM Plex Mono** — *every* number, score, date, count, label

The monospaced data layer is the texture of the whole design: counts stop
jittering as they change, and scores line up column-wise so they can be
compared by eye.

### Signature element: the match-signal meter

Each card carries five segments plus a printed score, rendering
`_relevance_score` from `pipeline/relevance.py`. A job scoring **1.00** (exact
phrase in title) reads instantly differently from one scoring **0.40** (query
words found only in the description). Hovering gives the reason in plain words
— "matched in description, not title".

The card's **left rail** goes solid green when the backend counted it an exact
match, muted otherwise. So a column of results can be triaged without reading a
single title.

This is the one bold element. Everything around it is deliberately quiet —
hairline rules, restrained pills, no gradients, no glow.

### Restraint

Cut from the old design: gradient stat cards, decorative colour on meta pills,
the 3-shadow elevation ramp on everything, colour-shift-only hover states.
Motion is now two things only — a 260ms card entrance and a 200ms tab
cross-fade. `prefers-reduced-motion` respected.

### Quality floor

- Responsive to mobile (sidebar becomes an off-canvas drawer under 900px)
- Visible keyboard focus rings on every interactive element
- `aria-label` on the signal meter; bars marked `aria-hidden`
- Bundle **19.04 kB CSS** (down from 23.03 kB) — smaller *and* a full redesign

---

## Verification

- **CSS class coverage checked programmatically**: every `className` used in
  any JSX file has a matching rule in the new stylesheet. No orphans, nothing
  silently unstyled.
- 0 compile errors · FastAPI boots · frontend builds in 2.08s
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**
- All functionality preserved: search, filters, pagination, job cards, sidebar
  nav, AI chat drawer, portal health modal, per-portal live fetch.

## Note

The two typefaces load from Google Fonts. If your deployment blocks external
font CDNs, self-host them or the stack falls back to system sans — the layout
holds either way, but the mono data layer is a real part of the design and is
worth keeping.

---
---

# ROUND 7 — UI reverted, AI search removed

## UI: back to the original look

`frontend/src/styles.css`, `index.html` and `JobCard.jsx` restored byte-for-byte
from the pre-redesign backup. The emerald/teal palette, Inter + Outfit
typography, and the original card layout are exactly as they were.

One block was appended afterwards, and only for elements that **did not exist**
in that original stylesheet and would otherwise have rendered completely
unstyled:

- skeleton loader and empty-state (`state-message`, `state-reasons`, `reason-count`, `state-hint`)
- live-scrape banner (`scrape-banner`)
- cached/live provenance chip (`cache-chip`)
- per-portal fetch result on the Platforms view (`portal-fetch-result`)
- card and tab entrance animations (`job-card-enter`, `tab-panel`, `fade-in-up`)
- portal health table (`health-table-wrapper`)

It reuses the existing palette variables — **no new colours, no new
typefaces**. Verified programmatically: every `className` used in any JSX file
now has a matching rule. CSS is 17.56 kB.

## AI search removed

**Why it "wasn't working": it was unreachable.** `Sidebar.jsx` declared an
`onOpenAiChat` prop and `App.jsx` passed it in — but the sidebar never rendered
a control that called it. There was no way to open the drawer from anywhere in
the UI, so the feature had never actually run.

Removed:
- `frontend/src/components/AiChatDrawer.jsx` — deleted
- its import, `isAiDrawerOpen` / `selectedJob` state, and render call in `App.jsx`
- the dead `onOpenAiChat` prop on `Sidebar`
- the now-unused `onSelectJob` prop threaded through `JobsGrid`
- `app.include_router(agent_router)` in `api/main.py` — the `/api/agent` routes
  no longer register (confirmed: `agent routes present: none`)

**Left in place and flagged:** the `agent/` package and `api/routes/agent.py`.
Only the route registration was removed. Deleting the package would also break
`tests/test_agent.py`, and a script outside the repo may import it. Say the
word and I'll remove it properly.

## Everything else unchanged — verified after the revert

```
date=past_12h     48      job_type=fulltime  1763
date=past_24h    232      job_type=contract   669
date=past_7d    1461      job_type=parttime    45
date=past_30d   2397      job_type=onsite    1615

match_quality: exact 77 of 89, yield 0.865, avg relevance 0.802
cache: working    portals diagnostics: 10    cache-stats: ok
```

- 0 compile errors · FastAPI boots · frontend builds in 1.96s
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**

---
---

# ROUND 8 — Filters made exact

## What was wrong, and it was my design decision

A "Past 24 hours" search was returning jobs whose own card read "Posted 2 days
ago". That is not a subtle bug — the number on the filter and the number on the
card openly contradicted each other, which makes the entire filter untrustworthy.

The cause was `PRECISION_TOLERANCE_MINUTES` in `pipeline/freshness.py`, which I
introduced deliberately:

```
PRECISION_DAY: 24 * 60      # a full extra day of slack
PRECISION_WEEK: 7 * 24 * 60 # a full extra week
```

My reasoning was that a source reporting only "1 day ago" is imprecise, so a job
sitting near the boundary shouldn't be dropped. That reasoning optimised for
returning more rows and ignored the thing that actually matters: **a filter has
to mean what it says.** I flagged it as a "remaining risk" instead of fixing it.
That was the wrong call.

## The fix

**All tolerances are now zero.** The invariant is now absolute:

> A job is returned only if the timestamp the UI DISPLAYS falls inside the
> requested window. No slack, no rounding grace. If a card says 2 days ago, it
> can never appear under a 24h filter.

Two changes:

1. `PRECISION_TOLERANCE_MINUTES` → all zeros. The table is kept rather than
   deleted so precision still travels with each row for DISPLAY (the UI can
   still distinguish an exact timestamp from an approximate one) — it just no
   longer buys a job its way past the filter.

2. `JobCard` now displays **the same date the filter used**. Previously the card
   showed only `posted_date`, so a job with no published date read "Posting date
   unknown" while silently qualifying on a first-seen timestamp the user could
   not see. It now reads "First seen 3 hours ago" in that case. The card can no
   longer disagree with the filter that returned it.

## Verified per job, not per count

Every returned job was checked individually against every filter:

```
past_12h  window=  12h  returned=  41  VIOLATIONS=0   oldest returned 11.2h
past_24h  window=  24h  returned= 226  VIOLATIONS=0   oldest returned 20.8h
past_7d   window= 168h  returned=1401  VIOLATIONS=0   oldest returned 88.9h
past_30d  window= 720h  returned=2378  VIOLATIONS=0   oldest returned 88.9h

platform=dice        returned= 236  violations=0
remote_only=true     returned= 775  violations=0
job_type=fulltime    returned=1747  violations=0
job_type=contract    returned= 665  violations=0
job_type=parttime    returned=  44  violations=0
job_type=onsite      returned=1603  violations=0
country=US           returned=1250  violations=0
title=Data Scientist returned= 170  violations=0
combo remote+US+24h  returned=  45  violations=0

TOTAL VIOLATIONS: 0  ->  ALL FILTERS EXACT
```

- 0 compile errors · FastAPI boots · frontend builds
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**

## The honest trade-off

12h and 24h now return fewer jobs, because that is how many genuinely fresh
jobs exist in the database. The old behaviour was not finding more jobs — it was
padding the count with stale ones.

If you want those windows genuinely fuller, the lever is **more frequent
scraping with source-side date scoping**, not a looser filter. That work is
already wired (`native_recency_hours` pushes `f_TPR` to LinkedIn, `fromage` +
`sort=date` to Indeed, `chips=date_posted` to SerpApi). What it needs is the
scheduler running the rotation more often — currently a full 20-keyword rotation
takes ~3.75 hours, which is thin for a 12h window. That costs API budget, so it
is your call, not mine to make silently.

---
---

# ROUND 9 — Stale results: triple-locked, plus a way to diagnose it on your box

## I could not reproduce a 30-day leak in this code

Built a controlled test (`/tmp/leak.py` logic) with synthetic jobs at exact
known ages — 1h, 10h, 20h, 30h, 3d, 10d, 30d, 60d — across every combination of
`posted_date` / `scraped_at` / `fetched_at` / precision, including the nastiest
case: **a 30-day-old job that was re-scraped one hour ago.**

```
past_12h  LEAKED: none    past_7d   LEAKED: none
past_24h  LEAKED: none    past_30d  LEAKED: none
TOTAL LEAKS: 0
```

The 30-day-old-but-refetched job is correctly excluded from 24h. So either the
running instance is on an older build, or something in the live data differs
from anything I can see. Rather than guess, I did two things.

## 1. Final hard sweep — a third, independent check

`/api/jobs` now re-checks the date window one last time, immediately before
building the response, trusting nothing upstream. SQL enforces it, the guard
enforces it, and now this enforces it again on the already-loaded dicts.

Why bother: a stale job reaching the user is the most damaging failure this app
can have — a "Past 24 hours" list containing a card reading "Posted 30 days ago"
destroys trust in every other filter simultaneously. Re-checking a few hundred
dicts costs microseconds.

If anything ever slips through again it now **dies here instead of reaching the
screen**, and logs at ERROR level with examples. The response also carries
`stale_blocked_by_final_sweep` — it should always be `0`, and it is never silent.

## 2. `GET /api/jobs/debug/freshness` — turns a guess into a line of output

Hit it with the exact same query params the UI sent. For every row it reports:

- the stored `posted_date`, `scraped_at`, `fetched_at`
- which field the filter actually used, and the precision
- the computed age in hours, and whether it passes
- the window it was compared against, plus the server's UTC clock

```
window=24.0h  sql_rows=5  would_be_blocked=0  oldest_passing=20.91h
  pass=True age=11.28h via=posted_date prec=day | ELECTRICAL ENGINEER
  pass=True age=14.91h via=posted_date prec=day | Software Engineer III
```

If a stale job shows up on your machine, run this and send me the output — it
will name the field and the age that let it through.

## A correction to something I told you earlier

In round 7 I reported "agent routes present: none" as proof the AI route was
gone. **That verification was unreliable.** I was enumerating `app.routes`, but
FastAPI 0.141 wraps included routers in `_IncludedRouter` and does not flatten
them into `app.routes` — so that listing shows only 9 entries no matter what is
registered. It briefly looked like the entire jobs API had vanished.

Re-verified properly, by issuing real requests:

```
200  /api/jobs                      200  /api/cache-stats
200  /api/jobs/debug/freshness      200  /api/portals/diagnostics
200  /api/date-filters              200  /api/scheduler/status
404  /api/agent/chat                404  /api/agent
```

Everything is registered; the agent routes really are gone. The conclusion was
right, the evidence I gave for it was not.

## Note for reproducing locally

`TestClient(app)` without a `with` block does **not** run the lifespan, so
`init_db()` never fires and `/api/jobs` fails with an `OperationalError` on a
missing table. Use `with TestClient(app) as c:`. This is a test-harness gotcha,
not an app bug — `uvicorn` always runs the lifespan.

## Verification

- 0 compile errors · all endpoints answer · frontend builds
- Controlled leak test: **0 leaks across all four windows**
- **13 failed, 48 passed — byte-identical failure list to the original
  untouched codebase. Zero regressions.**

---
---

# ROUND 10 — "3 hours ago" on a 30-day-old job

## The real bug, and it was mine

The card said **3 hours ago**. The job was a month old. The filter was never
the problem — **the date on the card was a lie.**

Three separate defects, all fabricating dates:

### 1. First-seen was being displayed as posting age

**629 of 2,943 rows have no published posting date.** For those, the only
timestamp we hold is when OUR scraper first saw the listing — which says
nothing about when it was posted. A job sitting on Dice for 30 days but first
crawled by us three hours ago has a first-seen age of 3h and a real age of 30
days.

In round 8 I "improved" the card to show `First seen 3 hours ago` for these,
so the displayed value would match the filtered value. That made the lie
**louder**, not quieter — it reads as "posted 3 hours ago". And
`is_fresh_enough` returned `True, "no_date_signal_unknown_passes"`, quietly
asserting a freshness we had no evidence for.

**Fixed:**
- Window **≤ 24h** → a job whose posting date was never published is now
  **excluded**. If freshness is the entire point of the filter, we do not guess.
- Window **> 24h** → first-seen is still allowed as a fallback, because over 7
  or 30 days discovery date is a reasonable proxy and excluding 629 rows would
  gut the results.
- The card now says **"Posting date not published"**, with the discovery time
  shown separately, muted, and labelled `we found it 3 hours ago`. The two can
  no longer be mistaken for each other.

### 2. `parse_date()` was inventing dates from real ones

| input | old result | now |
|---|---|---|
| `Today` | **yesterday** | today |
| `Monday, 18 July 2026` | **30 July** (18 days ago) | 18 July |
| `Sunday` | yesterday | correct calendar date |
| `a few hours ago` | **1 hour ago** | `None` (unknown) |

`"day" in val_lower` was checked before the explicit `today` case, and **every
weekday name contains "day"**. So `Monday, 18 July 2026` hit the relative
branch, `re.search` grabbed the first number it saw (18), and returned "18 days
ago" — turning an exact date into a wrong one.

Exact phrases are now matched first, weekday names are excluded from relative
parsing, and a relative phrase with no number returns `None`. **An honest
unknown beats a confident wrong answer.**

### 3. A pre-existing test was already catching this

`tests/test_date_24h_enforcement.py::test_past_24h_filter_enforcement` has been
failing since before I touched this codebase. It asserts exactly this: that a
24h filter returns *only* jobs genuinely within 24h.

**It now passes.** Test results went from `13 failed, 48 passed` to
`12 failed, 49 passed` — the one that flipped is precisely the 24h enforcement
test.

## Verified — every returned job, not counts

```
past_12h  window= 12h  returned=   4  no_published_date=0  VIOLATIONS=0
past_24h  window= 24h  returned=   8  no_published_date=0  VIOLATIONS=0
past_7d   window=168h  returned=1401 no_published_date=0  VIOLATIONS=0
past_30d  window=720h  returned=2378 no_published_date=0  VIOLATIONS=0

platform=dice        0 violations    job_type=parttime   0 violations
remote_only=true     0 violations    job_type=onsite     0 violations
job_type=fulltime    0 violations    title=Data Scientist 0 violations

TOTAL VIOLATIONS: 0  ->  ALL EXACT
```

Controlled synthetic test (jobs at exact known ages incl. a 30-day-old job
re-scraped an hour ago): **0 leaks across all four windows.**

- 0 compile errors · frontend builds · all endpoints answer
- **12 failed, 49 passed** — one *better* than the original codebase, and the
  improvement is the 24h enforcement test. No regressions.

## Read this before you look at the numbers

`past_24h` returns **8 jobs**. That is not the filter being broken — that is how
many jobs in your database have a **real, published posting date** inside 24
hours. Everything else in that window was either undated or older.

The previous number was larger because it was counting jobs we had no evidence
were fresh. **You cannot have both "only genuinely fresh jobs" and "lots of
jobs in a 24h window" from a database this size.**

The lever that actually raises it — and it is already built, just not turned up:

- Source-side date scoping is wired (`f_TPR` → LinkedIn, `fromage` + `sort=date`
  → Indeed, `chips=date_posted` → SerpApi), so each scrape already asks the
  portal for recent postings first.
- The scheduler runs **4 keywords per 45 minutes over a 20-keyword rotation** —
  a full pass takes **~3.75 hours**. For a 24h window that is only ~6 refreshes
  per keyword per window.

Raising `_KEYWORDS_PER_CYCLE` or shortening the rotation to the keywords you
actually search would directly multiply the 24h count. It costs API budget,
which is why I have not changed it without you saying so.

---
---

# ROUND 11 — Full verification against a real running server

Everything below was tested against an actual `uvicorn` process over HTTP, not
a test harness.

## 1. Fresh install on a blank database

```
TABLES CREATED: guard_audit_logs, jobs, run_logs, search_cache
posted_date_precision present: True
```

**Fixed while testing:** a blank database logged
`[Migration] posted_date_precision backfill skipped: no such table: jobs`.
Harmless (it was caught), but it appeared on every fresh install and looked
like a broken deployment. The lazy `ensure_migrated()` at import time runs
*before* `create_all()`, which is a legitimate state — it now checks whether
the table exists and returns quietly instead of logging a scary error.

## 2. Server boots clean

```
[Scheduler] APScheduler started with 4 scheduled crons
  (Pipeline A every 6h, 5-Tier every 45min, Max-Coverage every 6h,
   Retention purge daily 03:15 UTC)
Application startup complete.
```

## 3. Every endpoint over real HTTP

```
200  /                              200  /api/cache-stats
200  /docs                          200  /api/scrape-status
200  /api/date-filters              200  /api/portals/diagnostics
200  /api/job-titles                200  /api/scheduler/status
200  /api/stats                     200  /api/jobs
200  /api/status                    200  /api/jobs/debug/freshness
404  /api/agent/chat   <- correct, AI search removed

ENDPOINTS OK=13  BAD=0
```

## 4. Filter invariants, checked per job over HTTP

```
past_12h  total=    4  sweep_blocked=0  violations=0
past_24h  total=    8  sweep_blocked=0  violations=0
past_7d   total= 1369  sweep_blocked=0  violations=0
past_30d  total= 2378  sweep_blocked=0  violations=0

platform=dice          total= 236  violations=0
remote_only=true       total= 775  violations=0
job_type=parttime      total=  44  violations=0
job_type=onsite        total=1603  violations=0
title=Data Scientist   total= 170  violations=0

FILTER VIOLATIONS = 0        VERDICT: ALL GREEN
```

`sweep_blocked=0` on every window means no stale job even reached the final
guard — the SQL and guard layers are agreeing, not being rescued.

## 5. Caching works end to end

```
1st call: scrape=True   cache_miss_first_request
2nd call: scrape=False  cache_hit_within_1440min_window   <- zero paid API calls
match_quality: 42 exact of 51, yield 0.824, avg relevance 0.746
```

## 6. Frontend is served correctly by FastAPI

```
GET /                          200
/assets/index-*.js   181,558 bytes  200
/assets/index-*.css   17,619 bytes  200

date filters offered by API:  past_12h, past_24h, past_7d, past_30d
same four present in the built bundle;  past_10m absent
AI drawer removed from bundle: True
```

## 7. Test suite

```
12 failed, 49 passed
```

Compared to the original untouched codebase (`13 failed, 48 passed`), one test
moved from failing to passing:
`tests/test_date_24h_enforcement.py::test_past_24h_filter_enforcement`.
**No test that passed before fails now.**

## 8. Controlled leak test

Synthetic jobs at exact known ages (1h, 10h, 20h, 30h, 3d, 10d, 30d, 60d) across
every combination of `posted_date` / `scraped_at` / `fetched_at`, including a
30-day-old job re-scraped one hour ago:

```
TOTAL LEAKS: 0
```

---

## The one number to read carefully

`past_24h` returns **8 jobs**. That is not a broken filter — it is how many jobs
in your database have a **real published posting date** inside 24 hours.

That number is a property of your scrape cadence, not of the filter. The
scheduler currently runs **4 keywords per 45 minutes across a 20-keyword
rotation**, so a full pass takes **~3.75 hours**. Source-side date scoping is
already wired (`f_TPR` to LinkedIn, `fromage`+`sort=date` to Indeed,
`chips=date_posted` to SerpApi), so each scrape already asks portals for recent
postings first.

Raising `_KEYWORDS_PER_CYCLE` in `Scheduler/crons_jobs.py`, or trimming the
rotation to the keywords you actually search, multiplies that count directly.
It costs API budget, so I have not changed it without your say-so.

---
---

# ROUND 12 — FINAL VERIFICATION (the delivered zip, from scratch)

Everything below was run against the **shipped artifact** extracted into a
clean directory — not my working copy.

```
1. COMPILE ALL PYTHON        PASS  (0 errors)
2. BLANK DB SCHEMA           PASS  (jobs, run_logs, search_cache, guard_audit_logs)
3. SERVER BOOTS              PASS  (real uvicorn)
4. ALL ENDPOINTS 200         PASS  (12/12, /api/agent -> 404 as intended)
5. FILTER INVARIANTS         PASS  (0 violations)
6. CACHE (2nd call free)     PASS  (1st scrape=True, 2nd scrape=False)
7. FRONTEND SERVED           PASS  (assets 200, date options correct, AI drawer gone)

FINAL VERDICT: EVERYTHING WORKS
```

Filter detail:
```
past_12h  total=   4  sweep_blocked=0  violations=0
past_24h  total=   8  sweep_blocked=0  violations=0
past_7d   total=1361  sweep_blocked=0  violations=0
past_30d  total=2378  sweep_blocked=0  violations=0

platform=dice     236  violations=0     job_type=onsite   1603  violations=0
remote_only       775  violations=0     keyword            170  violations=0
job_type=parttime  44  violations=0
```

Controlled leak test on the shipped artifact: **TOTAL LEAKS: 0**.

## One apparent regression, investigated and explained

Running the tests on the extracted zip showed an extra failure that is not in
the baseline:

```
FAILED tests/test_new_platforms.py::test_apify_store_dynamic_check_found
```

It reproduced 3/3 on the zip and passed 3/3 on the original codebase, so I did
not dismiss it as flaky. Traced it:

`pipeline/apify_store.py:check_apify_actor_available()` returns `None`
immediately when `APIFY_API_TOKEN` is unset. **The zip ships without `.env`**
(secrets are deliberately excluded), so the token is absent and the function
short-circuits before the test's mock is ever reached.

Confirmed:
```
without APIFY token (as shipped)   -> 1 failed
with a dummy APIFY token set       -> 1 passed
```

**Not a code regression.** It is a pre-existing test smell: a fully-mocked test
that still depends on a real credential being present in the environment. On
your machine, where `.env` exists, it passes.

## Final test result, run the way it runs on your machine

```
12 failed, 49 passed
```

Against the original untouched codebase (`13 failed, 48 passed`), exactly one
test moved — from **failing to passing**:
`tests/test_date_24h_enforcement.py::test_past_24h_filter_enforcement`, the
test that asserts a 24h filter returns only jobs genuinely within 24 hours.

**No test that passed before fails now. Zero regressions, one improvement.**

## Two "MISSING" lines in the leak test that are correct behaviour

The controlled leak test reports two expected-but-absent jobs. Both are right:

- `J noposted seen1h` / `M no dates at all` absent from **past_24h** — jobs with
  no published posting date are now excluded from tight windows by design
  (round 10). The test's expectation list predates that change.
- `G posted 30d` absent from **past_30d** — it sits at *exactly* 720h, precisely
  on the cutoff. A boundary value, not a leak.

Neither is a stale job appearing where it shouldn't, which is what the test
exists to catch — and that count is **0**.

---
---

# ROUND 13 — Found it: the date-laundering machine

## Why my tests said "0 violations" and your screen said "4 hrs ago"

Both were true. The filter was doing its job correctly on the data it had —
**the data itself was wrong.** The card said 4 hours because the database said
4 hours, on a listing that had been up for 30 days.

## The mechanism

`pipeline/five_tier_orchestrator.py`, in the apply_url upsert path:

```python
if norm_job.posted_date > existing.posted_date:
    existing.posted_date = norm_job.posted_date   # <-- laundering
```

Every scrape, for a job already stored, if the portal now reported a **newer**
posting date, the stored one was **overwritten**.

Job boards routinely **bump or re-promote** old listings, and SerpApi / Google
Jobs then reports the re-promotion date rather than the original posting date.
So the cycle was:

1. Day 0 — we scrape a listing, store `posted_date = 30 days ago`. Correct.
2. Day 30 — the portal bumps it. The scrape reports "4 hours ago".
3. The upsert overwrites the stored date with "4 hours ago".
4. The job now **legitimately** passes a 24h filter, and the card **honestly**
   renders "4 hrs ago".

No layer was lying. The value had been corrupted at write time, days earlier.
My round-3 fuzzy-repost refresh did the same thing on a second code path.

## Fix 1 — `posted_date` is now write-once

A newer date for a URL we already hold is evidence of a **bump**, not of a new
posting. The earliest date we ever learned is kept.

The only write still allowed is **filling a genuine blank** — if we never knew
a posting date and now do, that is gaining information, not overwriting it.
Both code paths (apply_url upsert and fuzzy-repost refresh) now follow this.

`fetched_at` still records that we reconfirmed the listing is live; it is never
used as posting age inside a freshness-sensitive window.

Verified:
```
stored posted_date (30 days ago): 2026-07-18
portal now reports:               2026-08-17  ('4 hours ago')
stored posted_date AFTER rescrape:2026-07-18
overwritten by the bump? NO — correct
past_24h -> contains the 30-day job: no
```

## Fix 2 — Greenhouse was using `updated_at` as the posting date

`normalize_greenhouse_job` read `updated_at`, which Greenhouse bumps whenever
*anything* on the listing changes. A 30-day-old role edited this morning
therefore looked four hours old. Now reads `first_published` / `created_at` /
`published_at`, and falls back to `None` rather than to a field that means
something else.

## Fix 3 — your existing rows are still corrupted, so here is the cleanup

The write-once rule stops **new** corruption. Rows already laundered in your
database stay laundered. New script:

```bash
python scripts/audit_laundered_dates.py            # report only
python scripts/audit_laundered_dates.py --fix      # reset them to unknown
```

**The tell it uses:** `posted_date` newer than `scraped_at`. `scraped_at` is
written once at insert and never touched, so a row we first saw 30 days ago
cannot genuinely have been posted 4 hours ago — that value can only have
arrived by overwrite.

Proven end to end on an injected laundered row:
```
suspicious rows: 1
  LAUNDERED cloud engineer   posted=2026-08-17  first_seen=2026-07-18  (+30d)

present in past_24h BEFORE fix: True
FIXED: reset 1 row(s) to NULL (unknown)
present in past_24h AFTER fix:  False
```

`--fix` sets the date to NULL rather than guessing. Those rows then read
"Posting date not published" and are excluded from 12h/24h windows — the
correct treatment for a date we cannot trust.

## Verification

- 0 compile errors · bump test passes · controlled leak test: **0 leaks**
- **12 failed, 49 passed** — one better than the original codebase
  (`13 failed, 48 passed`), the improvement being the 24h enforcement test.
  No test that passed before fails now.

## Run this first, on your live database

```bash
python scripts/audit_laundered_dates.py
```

Whatever number it reports is how many of your stored posting dates were
rewritten by portal bumps. That number is the answer to "why was it showing
4 hrs ago on a 30-day-old job".
