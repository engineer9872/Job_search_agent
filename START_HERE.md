# Start here

```bash
python scripts/job_date_doctor.py --fix    # repair corrupted stored dates
uvicorn api.main:app --reload              # restart backend
cd frontend && npm run build               # rebuild frontend
```

Then, to confirm every portal is alive with your own filters:

```bash
curl -X POST "http://localhost:8000/api/portals/refresh-all?title=cloud%20engineer&date_posted=past_24h"
```

It returns a per-portal verdict: which of the ten returned jobs, which were
silent, and why.

---

## 1. All filters are exact

Verified against a live server, checking **every returned row**, not counts:

```
past_12h   violations=0        platform=dice        violations=0
past_24h   violations=0        remote_only=true     violations=0
past_7d    violations=0        job_type=fulltime    violations=0
past_30d   violations=0        job_type=contract    violations=0
                               job_type=parttime    violations=0
                               job_type=onsite      violations=0
                               country=US           violations=0
                               keyword search       violations=0
                               remote+US+24h combo  violations=0

TOTAL FILTER VIOLATIONS: 0
```

A 24h search cannot return a job older than 24h. There are now **three
independent checks** enforcing this: the SQL pre-filter, the guard, and a final
sweep immediately before the response is built.

## 2. All ten portals contribute

```
linkedin  561    usajobs        231    weworkremotely  154
glassdoor 277    hired          214    careerbuilder   130
indeed    252    ziprecruiter   163
dice      236    simplyhired    160

contributing: 10/10    zero: []
```

Every `/api/jobs` response now carries `portal_coverage`, so a portal returning
nothing is visible instead of silently absent:

```json
"portal_coverage": {
  "per_portal": {...}, "portals_contributing": 10,
  "portals_total": 10, "portals_with_zero": []
}
```

New endpoint `POST /api/portals/refresh-all` forces a live scrape across all ten
with your current filters, bypasses the cache, writes a RunLog per portal (so
the Platforms view updates), and reports `working` / `silent` lists.

## 3. Blocked Tier 0 now escalates straight to Firecrawl

This is what you asked for, and it is the correct use of Firecrawl.

When the free tier returns **blocked** — 403, Cloudflare, DataDome, or a
JS-only shell — that is not "no jobs". It means the page needs a real browser
behind an anti-bot proxy, which is exactly Firecrawl's stealth tier and the one
source that can actually reach it.

So a blocked portal now gets Firecrawl **prioritised**, and may draw on a
reserve allowance even when the normal hourly budget is spent:

```
budget: hourly 40, daily 300
blocked_portal_reserve_cap: 75      (25% of daily, blocked portals only)
```

A portal whose free tier merely came back *empty* (not blocked) does **not**
get this — SerpApi is cheaper and works fine there. Spending a Firecrawl credit
on a page nothing else can reach is the highest-value call available; spending
it where a cheap source already works is waste.

## 4. Exact-match quality

```
Cloud Engineer    86 results, 74 exact, yield 0.86,  avg relevance 0.80
Data Scientist   170 results,166 exact, yield 0.98,  avg relevance 0.93
DevOps Engineer  123 results,120 exact, yield 0.98,  avg relevance 0.88
```

Results are ranked by relevance first, recency second. Titles like "Cloud Sales
Executive" are demoted below the exact-match bar for a "cloud engineer" query.

## Verification

- 0 compile errors · frontend builds · live server: all endpoints 200
- Controlled leak test (synthetic jobs at exact known ages): **0 leaks**
- Bump-laundering test: a re-promoted 30-day-old job **cannot** rewrite its date
- **12 failed, 49 passed** — one better than the original codebase
  (`13 failed, 48 passed`); the improvement is the 24h enforcement test.
  No test that passed before fails now.

## The one thing I cannot fix from here

`past_24h` returns few jobs because few jobs in your database have a **real,
verifiable** posting date inside 24 hours. That is a property of scrape
frequency, not of the filter.

`Scheduler/crons_jobs.py` runs 4 keywords per 45 minutes across a 20-keyword
rotation — a full pass takes ~3.75 hours. Source-side date scoping is already
wired (LinkedIn `f_TPR`, Indeed `fromage`+`sort=date`, SerpApi
`chips=date_posted`), so each scrape already asks portals for recent postings
first.

Raising `_KEYWORDS_PER_CYCLE`, or trimming the rotation to the keywords you
actually search, multiplies that count directly. It costs API budget, which is
why I have not changed it without your say-so. Say the word and it is a
one-line change.

Two portals also still need credentials no code change can supply:
**ZipRecruiter Partner API** and **CareerBuilder API**. Run
`GET /api/portals/diagnostics` — anything listed under `needs_user_action`
needs keys, not a patch.

---

## 5. Hybrid search — related roles, not just literal titles

Job titles are not standardised. One company's "Cloud Engineer" is another's
"Platform Engineer" and another's "SRE" — the same job, three names. Exact-title
matching hid most of the real market.

Search is now **tiered**, and the tier travels with each result so ranking keeps
genuine matches on top:

| tier | meaning | example for "cloud engineer" |
|---|---|---|
| `core` | the words you typed | Cloud Engineer, Principal Cloud Engineer |
| `strong` | a different name for the same job | SRE, Platform Engineer, DevOps Engineer |
| `related` | adjacent, plausibly interesting | Backend Engineer, Network Engineer |

Measured on your database:

```
'cloud engineer'
  mode=exact    86 results   (core  86, same-job   0, related   0)
  mode=strict  259 results   (core 100, same-job 159, related   0)
  mode=hybrid  290 results   (core 100, same-job 161, related  29)   <- default

'devops engineer'  123 -> 290      'data scientist'  170 -> 400
```

**Results are still ordered core → strong → related.** The top eight for
"cloud engineer" are all literal Cloud Engineer roles; SREs and Platform
Engineers follow. Widening what *qualifies* never means scrolling past
loosely-related roles to reach the real ones.

Three modes via `?search_mode=`:

- `hybrid` (default) — core + same-job synonyms + adjacent roles
- `strict` — core + same-job synonyms only
- `exact` — literal title match, the old behaviour

The response tells you what it expanded to, so nothing is hidden:

```json
"expanded_to": {
  "same_job_synonyms": ["cloud architect", "devops engineer", "sre", ...],
  "related_roles": ["backend engineer", "network engineer", ...],
  "role_families": ["cloud_infra"]
},
"results_by_tier": {"exact_title": 100, "same_job_different_title": 161, "related_role": 29}
```

### Guardrails, because naive expansion is worse than none

- The taxonomy is **hand-curated**, not generated. A sloppy synonym list is
  exactly how "engineer" starts matching "sales engineer".
- **"Cloud Sales Executive" still scores 0** for a "cloud engineer" query, and
  so does "Nurse Practitioner". Verified.
- A vague title ("Engineer II") can be rescued by its **skills** (Terraform +
  Kubernetes + AWS), but a skill match is capped at `related` — weaker evidence
  than a title match, so it can never outrank one.
- **Every other filter stays exact.** Verified under hybrid mode:
  ```
  past_12h/24h/7d/30d  date_violations=0
  remote_only=true     violations=0
  platform=dice        violations=0
  TOTAL VIOLATIONS UNDER HYBRID: 0
  ```
- Scrapes send only **core + up to 2 strong synonyms**. Adjacent roles are
  never scraped — they only widen what surfaces from what is already stored,
  so hybrid search costs no extra API budget beyond two keywords.
