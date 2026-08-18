import logging
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, and_

from DB import get_db, Job, RunLog
from pipeline import run_pipeline, get_last_pipeline_metrics
from pipeline.fallback_pipeline import run_4layer_pipeline, load_portals_config
from pipeline.filter_lock import create_locked_filter_spec
from pipeline.filter_guard import (
    ThreeTierFilterGuard,
    canonical_job_type,
    GLOBAL_COUNTRY_VALUES,
)
from pipeline.query_parser import parse_search_terms, build_combined_scrape_keyword
from pipeline.date_filters import (
    resolve_cutoff_minutes,
    is_freshness_sensitive,
    UI_DATE_FILTER_VALUES,
)
from pipeline.freshness import build_sql_freshness_clause
from pipeline import search_cache as sc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Jobs"])

# How long the HTTP request waits on a live scrape before returning what is
# already in the DB. The scrape thread keeps running past this.
LIVE_FETCH_TIMEOUT_SECONDS = 12


@router.get("/job-titles")
def get_job_titles():
    """Preset chips shown in the UI -- purely convenience shortcuts. The
    backend does NOT restrict matching to this list; any text typed by the
    user is parsed dynamically (see pipeline/query_parser.py)."""
    return [
        "Software Engineer", "Data Scientist", "Machine Learning Engineer",
        "AI Engineer", "DevOps Engineer", "Cloud Engineer / Architect",
        "Product Manager", "Data Engineer", "QA / Test Automation Engineer",
        "Site Reliability Engineer (SRE)", "Cybersecurity Engineer", "ServiceNow Engineer",
    ]


@router.get("/date-filters")
def get_date_filters():
    """
    The date_posted options the UI should render. Exposed so the frontend and
    backend can never drift apart on which windows are supported.
    """
    labels = {
        "past_12h": "Past 12 Hours",
        "past_24h": "Past 24 Hours",
        "past_7d": "Past 7 Days",
        "past_30d": "Past 30 Days",
    }
    return [{"value": v, "label": labels.get(v, v)} for v in UI_DATE_FILTER_VALUES]


def _serialize_job(j: Job) -> dict:
    return {
        "id": j.id,
        "title": j.title,
        "canonical_title": j.canonical_title,
        "company": j.company,
        # AUDIT FIX: `skills` is part of the SQL `q` filter, so the guard
        # needs it too or the two layers disagree on any company/skill search.
        "skills": j.skills,
        "city": j.city,
        "country": j.country,
        "salary_min": j.salary_min,
        "salary_max": j.salary_max,
        "currency": j.currency,
        "remote_flag": j.remote_flag,
        "job_type": j.job_type,
        "source_platform": j.source_platform,
        "platform_id": j.source_platform,
        "apply_url": j.apply_url,
        "description_snippet": j.description_snippet,
        "posted_date": j.posted_date.isoformat() if j.posted_date else None,
        "posted_date_precision": getattr(j, "posted_date_precision", None),
        "fetched_at": j.fetched_at.isoformat() if j.fetched_at else None,
        # Needed by pipeline/freshness.py: scraped_at is first-sight (never
        # bumped), fetched_at is last-confirmed (bumped on every refresh).
        "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
        "recruiter_name": getattr(j, "recruiter_name", None),
        "recruiter_email": j.recruiter_email,
        "work_authorization_note": getattr(j, "work_authorization_note", None),
    }


# ---------------------------------------------------------------------------
# SQL PRE-FILTER
#
# PART 1 AUDIT: this function mirrors pipeline/filter_guard.py check-for-check.
# Any change here MUST be mirrored there. The guard is still the final source
# of truth, but the two must never DISAGREE -- a job SQL drops never reaches
# the guard at all, so a stricter-than-guard SQL layer silently loses results.
# ---------------------------------------------------------------------------
def _build_job_query(
    db: Session,
    title: Optional[str],
    platform: Optional[str],
    country: Optional[str],
    remote_bool: bool,
    job_type: Optional[str],
    date_posted: Optional[str],
    q: Optional[str],
    search_mode: Optional[str] = "hybrid",
):
    query = db.query(Job)

    # --- title / keyword: HYBRID SEARCH -------------------------------------
    # The SQL layer widens to the expanded term set (core + strong synonyms +
    # adjacent roles) so a "cloud engineer" search can surface an "SRE" or a
    # "Platform Engineer" -- different names for the same job. The guard then
    # classifies each candidate into a tier and relevance ranking keeps genuine
    # matches above the adjacent ones, so widening here does not mean the user
    # scrolls through loosely-related roles to find the real ones.
    if title and isinstance(title, str) and title.strip().lower() != "all":
        from pipeline.query_expansion import expand_query, all_search_terms
        _exp = expand_query(title, mode=search_mode or "hybrid")
        _terms = [t for t, _tier in all_search_terms(_exp)]
        if _terms:
            conditions = []
            for t_str in _terms:
                conditions.append(Job.canonical_title.ilike(f"%{t_str}%"))
                conditions.append(Job.title.ilike(f"%{t_str}%"))
            # A skill hit can identify a vaguely-titled role. Kept out of the
            # title match and confined to the skills column so it cannot pull
            # in unrelated jobs that merely mention a technology in prose.
            for sk in _exp.get("skills", [])[:8]:
                conditions.append(Job.skills.ilike(f"%{sk}%"))
            query = query.filter(or_(*conditions))

    # --- platform: exact ----------------------------------------------------
    if platform and isinstance(platform, str) and platform.strip().lower() not in ["all", ""]:
        query = query.filter(Job.source_platform == platform.strip().lower())

    # --- remote_only --------------------------------------------------------
    if remote_bool is True:
        query = query.filter(Job.remote_flag == True)  # noqa: E712

    # --- country ------------------------------------------------------------
    # AUDIT FIX: the old query OR'd in `remote_flag == True`, so country=US
    # returned a Germany-only remote role. A remote job now qualifies only
    # when its country is genuinely UNKNOWN or explicitly GLOBAL -- matching
    # ThreeTierFilterGuard._check_country exactly.
    if country and isinstance(country, str) and country.strip().lower() not in ["all", ""]:
        c_code = country.strip().upper()
        query = query.filter(
            or_(
                func.upper(Job.country) == c_code,
                Job.country.is_(None),
                Job.country == "",
                func.upper(Job.country).in_(sorted(GLOBAL_COUNTRY_VALUES)),
            )
        )

    # --- job_type -----------------------------------------------------------
    jt_canon = canonical_job_type(job_type)
    if jt_canon not in [None, "all"]:
        if jt_canon == "onsite_only":
            # AUDIT FIX [FIX-1]: "onsite" is a LOCATION constraint, not an
            # employment type. The old guard compared it as a job_type string,
            # which could never match -- every onsite search returned zero.
            query = query.filter(Job.remote_flag == False)  # noqa: E712
        else:
            # AUDIT FIX [FIX-2] + RULE 5: NULL / "" / "unknown" job_type must
            # PASS -- a missing field is not a confirmed conflict.
            query = query.filter(
                or_(
                    Job.job_type == jt_canon,
                    Job.job_type.is_(None),
                    Job.job_type == "",
                    func.lower(Job.job_type) == "unknown",
                )
            )

    # --- date_posted --------------------------------------------------------
    # AUDIT FIX [FIX-3]: posted_date is authoritative WHEN KNOWN; fetched_at is
    # only a fallback for rows with no posted_date. The old query let a
    # 40-day-old posting satisfy a 7-day filter purely because we re-scraped it
    # today.
    # AUDIT FIX [FIX-4]: `posted_date` is a naive column while `fetched_at` /
    # `scraped_at` are DateTime(timezone=True). One naive cutoff compared
    # against all three is silently fine on SQLite but raises on PostgreSQL.
    # Delegated to pipeline/freshness.py -- the SAME module the guard calls.
    # The clause it returns is deliberately a SUPERSET of the guard's
    # predicate (it grants the widest tolerance to every row), because SQL is
    # a cheap pre-filter and the guard is the source of truth. A SQL layer
    # stricter than the guard would drop rows before the guard ever saw them.
    query = query.filter(
        build_sql_freshness_clause(Job, resolve_cutoff_minutes(date_posted))
    )

    # --- free-text q --------------------------------------------------------
    if q and isinstance(q, str) and q.strip():
        q_term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Job.title.ilike(q_term),
                Job.canonical_title.ilike(q_term),
                Job.company.ilike(q_term),
                Job.skills.ilike(q_term),
                Job.description_snippet.ilike(q_term),
            )
        )

    return query


def _run_scrape(live_keyword: str, live_country: str, remote_bool: bool,
                target_portals, since_hours: Optional[int], cache_key: str) -> dict:
    """
    Submits the orchestrator run to the process-wide scrape manager and waits
    only up to the first-response deadline.

    This REPLACES a `with concurrent.futures.ThreadPoolExecutor(...)` block
    that could never return early: the context manager calls
    shutdown(wait=True) on exit, so the old "12 second timeout" logged a
    reassuring message and then blocked for the full scrape anyway (measured:
    timeout logged at 2.0s, block exited at 6.0s on a 6s task).
    """
    from pipeline.five_tier_orchestrator import run_five_tier_orchestrator
    from pipeline.scrape_jobs import scrape_manager

    return scrape_manager.submit_and_wait(
        cache_key,
        lambda: run_five_tier_orchestrator(
            keyword=live_keyword,
            country=live_country,
            remote_only=remote_bool,
            portals=target_portals,
            since_hours=since_hours,
        ),
    )


@router.get("/jobs")
def get_jobs(
    q: Optional[str] = Query(None, description="Free-text search across title, company, skills, or description"),
    title: Optional[str] = Query(None, description="Comma-separated job title(s)/keyword(s), any free text"),
    platform: Optional[str] = Query(None, description="Exact platform_id"),
    remote_only: Optional[bool] = Query(False),
    country: Optional[str] = Query(None, description="Exact ISO 2-letter country code"),
    job_type: Optional[str] = Query(None),
    date_posted: Optional[str] = Query(None, description="past_12h | past_24h | past_7d | past_30d"),
    search_mode: Optional[str] = Query(
        "hybrid",
        description="hybrid = also show related roles (default) | strict = same-job synonyms only | exact = literal title match",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    On-demand search endpoint.

    FLOW:
      1. Build the SQL pre-filter (mirrors the guard exactly -- see Part 1).
      2. Consult SearchCache for this EXACT normalized filter combination.
      3. Inside the bucket's serve window -> answer from the DB, ZERO paid
         API calls.
      4. Otherwise trigger a scrape (FULL for 12h/24h, 24h-DELTA for 7d/30d),
         stamp last_scraped_at, then re-query and answer.
      5. ThreeTierFilterGuard is the final source of truth in every path.
    """
    from pipeline.filter_lock import parse_bool
    r_bool = parse_bool(remote_only)

    # ---------- Build the FilterSpec (validated + hash-locked) -------------
    # NOTE: q is NO LONGER merged into job_title. Merging it forced the guard
    # to demand the q term appear in the job TITLE, while SQL searched
    # title/company/skills/description -- so searching a company name matched
    # in SQL and was then rejected by the guard. q now travels in its own
    # field and the guard validates it across the same fields SQL does.
    title_terms_raw = []
    if title and title.strip().lower() != "all":
        title_terms_raw.extend([t.strip() for t in title.split(",") if t.strip()])
    combined_title_field = ",".join(title_terms_raw) if title_terms_raw else "all"

    try:
        filter_spec = create_locked_filter_spec(
            job_title=combined_title_field,
            platform=platform,
            country=country,
            remote_only=remote_only,
            date_posted=date_posted,
            job_type=job_type,
            q=q,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _query_rows():
        return _build_job_query(
            db, title, platform, country, r_bool, job_type, date_posted, q, search_mode
        ).order_by(Job.posted_date.desc().nullslast(), Job.fetched_at.desc()).all()

    def _verified(guard_obj):
        return guard_obj.process_guard_checks([_serialize_job(j) for j in _query_rows()])

    from pipeline.query_expansion import expand_query as _expand
    _expansion = _expand(combined_title_field if title_terms_raw else "",
                         mode=search_mode or "hybrid")

    guard = ThreeTierFilterGuard(filter_spec, expansion=_expansion)
    all_verified_jobs = _verified(guard)

    # ---------- Scrape keyword / scope -------------------------------------
    scrape_terms = list(title_terms_raw)
    if q and q.strip():
        scrape_terms.append(q.strip())
    term_groups = parse_search_terms(scrape_terms)
    has_keyword_filter = len(term_groups) > 0

    target_portals = None
    if platform and isinstance(platform, str) and platform.strip().lower() not in ["all", ""]:
        target_portals = [platform.strip().lower()]

    # ---------- PART 2: cache-aware scrape decision ------------------------
    filter_dict = sc.build_filter_dict(
        title=title, q=q, platform=platform, country=country,
        remote_only=r_bool, job_type=job_type, date_posted=date_posted,
    )
    decision = sc.evaluate_cache(db, filter_dict, date_posted)

    # A scrape only makes sense when the user gave SOMETHING to scrape for.
    # With no keyword and no platform there is no query to send to a portal.
    scrapeable = has_keyword_filter or bool(target_portals)
    if not scrapeable and decision.should_scrape:
        decision.should_scrape = False
        decision.reason = "no_keyword_or_platform_to_scrape"

    scrape_actually_ran = False
    scrape_status = "not_triggered"

    if decision.should_scrape:
        # PART 3: in-flight de-duplication. If an identical scrape is already
        # running, skip -- do not fire a second paid call for the same key.
        if not sc.scrape_registry.try_acquire(decision.cache_key):
            decision.should_scrape = False
            decision.reason = "skipped_duplicate_scrape_already_in_flight"
            logger.info(
                f"[Live Fetch] Skipping duplicate scrape for key={decision.cache_key[:12]}... "
                f"-- an identical scrape is already in flight."
            )
        else:
            try:
                live_country = (country or "in").strip().lower()
                if live_country in ["all", ""]:
                    live_country = "in"
                # HYBRID SCRAPE: also ask the portals for the strongest
                # synonyms, capped -- each extra keyword is another paid call.
                # Adjacent/related roles are deliberately NOT scraped; they
                # only widen what we surface from what is already stored.
                from pipeline.query_expansion import build_scrape_keywords
                _kw_list = build_scrape_keywords(_expansion, max_terms=3)
                live_keyword = (
                    " OR ".join(_kw_list) if len(_kw_list) > 1
                    else build_combined_scrape_keyword(term_groups)
                )

                logger.info(
                    f"[Live Fetch] keyword='{live_keyword}' bucket={decision.bucket} "
                    f"mode={decision.refresh_mode} since_hours={decision.since_hours} "
                    f"reason={decision.reason} portals={target_portals or 'all 10'}"
                )

                # PART 3.3: always give the source a recency scope, not just
                # on delta runs. A 12h/24h search does a FULL re-scrape
                # (since_hours is None for cache purposes), but the QUERY sent
                # to each portal must still be date-scoped and date-sorted --
                # otherwise the portal returns its default unsorted page and
                # almost everything gets discarded by our own filter, which is
                # why tight windows came back nearly empty.
                from pipeline.date_filters import get_cache_policy
                _native = get_cache_policy(date_posted).get("native_recency_hours")
                _scope_hours = decision.since_hours or _native

                _scrape_outcome = _run_scrape(
                    live_keyword, live_country, bool(r_bool),
                    target_portals, _scope_hours, decision.cache_key,
                )
                scrape_status = _scrape_outcome["status"]
                scrape_actually_ran = True

                # Stamp last_scraped_at even when we returned before the
                # scrape finished. The job is still running on the module-level
                # executor and WILL commit its rows; firing a fresh paid scrape
                # on the user's very next click is exactly what this system
                # exists to prevent.
                sc.record_scrape(
                    db, decision.cache_key, filter_dict,
                    decision.bucket, decision.refresh_mode or "full",
                )

                db.expire_all()
                guard = ThreeTierFilterGuard(filter_spec, expansion=_expansion)
                all_verified_jobs = _verified(guard)
            except Exception as e:
                logger.error(f"[Live Fetch] Failed to execute live orchestrator run: {e}")
            finally:
                # Only release when the scrape genuinely completed. Releasing
                # while it is still running would let a concurrent request
                # fire a duplicate paid scrape for the same key.
                if scrape_status != "in_progress":
                    sc.scrape_registry.release(decision.cache_key)
    else:
        sc.record_cache_hit(db, decision.cache_key)

    # ---------- Freshness capability note ----------------------------------
    _freshness_capability_note = None
    if is_freshness_sensitive(date_posted) and target_portals:
        from pipeline.capabilities import can_honor_freshness_window
        if not can_honor_freshness_window(target_portals[0], resolve_cutoff_minutes(date_posted)):
            _freshness_capability_note = (
                f"Platform '{target_portals[0]}' cannot reliably confirm postings within the "
                f"requested window; showing best-available cached results instead of live data."
            )

    # ---------- Empty-state diagnosis (feeds the UI, spec Part 5) ----------
    empty_reason = None
    if not all_verified_jobs:
        candidates_seen = getattr(guard, "last_candidate_count", 0)
        reasons = getattr(guard, "last_rejection_reasons", {}) or {}
        if candidates_seen == 0:
            empty_reason = {
                "code": "no_candidates_in_db",
                "message": (
                    "Nothing in the database matches this filter combination yet"
                    + (" -- a live scrape was just triggered, try again in a few seconds."
                       if scrape_actually_ran else ".")
                ),
                "top_rejection_reasons": {},
            }
        else:
            top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]
            empty_reason = {
                "code": "all_candidates_filtered_out",
                "message": (
                    f"{candidates_seen} job(s) matched the broad search but none satisfied "
                    f"every filter you set."
                ),
                "top_rejection_reasons": dict(top),
            }

    # =====================================================================
    # FINAL HARD SWEEP — LAST LINE OF DEFENCE ON FRESHNESS
    #
    # The SQL layer and the guard already enforce the date window. This sweep
    # enforces it a THIRD time, immediately before the response is built, and
    # it trusts nothing upstream.
    #
    # Rationale: a stale job reaching the user is the single most damaging
    # failure this app can have -- a "Past 24 hours" list containing a card
    # that reads "Posted 30 days ago" destroys trust in every other filter at
    # the same time. Re-checking a few hundred already-loaded dicts costs
    # microseconds. If any code path anywhere ever lets one through again,
    # it dies here instead of reaching the screen.
    #
    # A job with NO date signal at all still passes (unknown is not a
    # confirmed conflict) -- but it is counted separately so it is visible.
    # =====================================================================
    from pipeline.freshness import is_fresh_enough as _fresh
    _window = resolve_cutoff_minutes(date_posted)
    _swept, _dropped_stale = [], []
    for _j in all_verified_jobs:
        _ok, _why = _fresh(_j, _window)
        if _ok:
            _swept.append(_j)
        else:
            _dropped_stale.append({"title": _j.get("title"), "reason": _why})
    if _dropped_stale:
        logger.error(
            f"[FINAL SWEEP] Blocked {len(_dropped_stale)} stale job(s) that passed BOTH the "
            f"SQL layer and the guard for window={_window}min. This indicates an upstream "
            f"filter bug -- examples: {_dropped_stale[:3]}"
        )
    all_verified_jobs = _swept

    # RELEVANCE RANKING: order by how well each job actually matches what the
    # user typed, then by recency. Without this a portal that happens to return
    # first dominates page 1 regardless of match quality -- which is how a
    # "cloud engineer" search ends up showing "Cloud Sales Executive" at the
    # top. Each job carries its score so the UI can show WHY it ranked there.
    from pipeline.relevance import rank_jobs, evaluate_batch
    from pipeline.query_expansion import classify_match, TIER_CORE, TIER_STRONG, TIER_RELATED
    _query_text = ", ".join(scrape_terms) if scrape_terms else ""
    if _query_text:
        all_verified_jobs = rank_jobs(all_verified_jobs, _query_text)
        _match_metrics = evaluate_batch(all_verified_jobs, _query_text)

        # HYBRID TIERS. Every job carries how it matched, and the final sort
        # puts core matches above same-job synonyms above adjacent roles. A
        # user searching "cloud engineer" sees Cloud Engineers first, then SREs
        # and Platform Engineers, then adjacent roles -- rather than an
        # undifferentiated pile.
        _tier_counts = {TIER_CORE: 0, TIER_STRONG: 0, TIER_RELATED: 0}
        for _j in all_verified_jobs:
            _tier, _w, _term = classify_match(_j, _expansion)
            _j["_match_tier"] = _tier
            _j["_matched_term"] = _term
            if _tier in _tier_counts:
                _tier_counts[_tier] += 1
        _tier_order = {TIER_CORE: 0, TIER_STRONG: 1, TIER_RELATED: 2, "none": 3}
        all_verified_jobs.sort(
            key=lambda j: (_tier_order.get(j.get("_match_tier"), 3),
                           -float(j.get("_relevance_score") or 0))
        )

        match_quality = {
            "query": _query_text,
            "search_mode": search_mode or "hybrid",
            "exact_matches": _match_metrics["exact_matches"],
            "total": _match_metrics["total"],
            "match_yield": _match_metrics["match_yield"],
            "avg_relevance": _match_metrics["avg_score"],
            "expanded_to": {
                "same_job_synonyms": _expansion.get("strong", []),
                "related_roles": _expansion.get("related", []),
                "role_families": _expansion.get("families", []),
            },
            "results_by_tier": {
                "exact_title": _tier_counts[TIER_CORE],
                "same_job_different_title": _tier_counts[TIER_STRONG],
                "related_role": _tier_counts[TIER_RELATED],
            },
        }
    else:
        match_quality = None

    # PER-PORTAL COVERAGE. Shows how many of the results came from each of the
    # 10 portals for THIS request, and names the ones that contributed nothing.
    # Without this, "are all portals working?" is unanswerable from the UI --
    # a portal returning zero looks identical to a portal never being tried.
    _coverage = {p.get("id"): 0 for p in load_portals_config()}
    for _j in all_verified_jobs:
        _pid = _j.get("source_platform")
        if _pid in _coverage:
            _coverage[_pid] += 1
    portal_coverage = {
        "per_portal": _coverage,
        "portals_contributing": sum(1 for v in _coverage.values() if v > 0),
        "portals_total": len(_coverage),
        "portals_with_zero": [k for k, v in _coverage.items() if v == 0],
    }

    page_jobs = all_verified_jobs[offset: offset + limit]

    return {
        "total": len(all_verified_jobs),
        "limit": limit,
        "offset": offset,
        "filter_hash": filter_spec.integrity_hash,
        "jobs": page_jobs,
        "live_fetch_triggered": scrape_actually_ran,
        # "in_progress" means the scrape is STILL RUNNING and more jobs are
        # landing. The client must poll /api/scrape-status/{cache_key} and
        # re-request once it reports complete -- treating this response as
        # final is exactly the bug that made fetched jobs never appear.
        "scrape_status": scrape_status,
        "scrape_poll_key": decision.cache_key if scrape_status == "in_progress" else None,
        "cache": decision.to_dict(),
        # How many returned jobs genuinely match the query, not just how many
        # rows came back. This is also what drives paid-tier escalation --
        # see pipeline/escalation.py.
        "match_quality": match_quality,
        "portal_coverage": portal_coverage,
        # Non-zero means an upstream filter let a stale job through and the
        # final sweep caught it. Should always be 0; surfaced so it is never
        # silent.
        "stale_blocked_by_final_sweep": len(_dropped_stale),
        "empty_reason": empty_reason,
        "freshness_capability_note": _freshness_capability_note,
    }


@router.get("/scrape-status/{cache_key}")
def get_scrape_status(cache_key: str, db: Session = Depends(get_db)):
    """
    Poll target for a scrape that outlived its HTTP request.

    Returns `status` of in_progress | complete | failed | unknown. The client
    polls this (~every 3s) after receiving scrape_status="in_progress" from
    /api/jobs, and re-requests the job list once it reports complete.
    """
    from pipeline.scrape_jobs import scrape_manager

    st = scrape_manager.get_status(cache_key)
    if st is None:
        # Not tracked: either it finished long enough ago to be swept, or the
        # process restarted. Either way the client should just re-query.
        return {
            "cache_key": cache_key,
            "status": "unknown",
            "should_refresh": True,
            "message": "No tracked scrape for this key; re-run the search to see current results.",
        }

    if st["status"] == "complete":
        res = st.get("result") or {}
        st["should_refresh"] = True
        st["inserted_count"] = res.get("inserted_count")
        st["total_raw_fetched"] = res.get("total_raw_fetched")
        st["message"] = (
            f"Scrape finished in {st['elapsed_seconds']}s; "
            f"{res.get('inserted_count', 0)} new job(s) inserted."
        )
    elif st["status"] == "in_progress":
        st["should_refresh"] = False
        st["message"] = f"Scrape running ({st['elapsed_seconds']}s elapsed)\u2026"
    else:
        st["should_refresh"] = True
        st["message"] = f"Scrape failed: {st.get('error')}"

    return st


@router.get("/scrape-status")
def get_scrape_manager_stats():
    """Diagnostics for the background scrape executor."""
    from pipeline.scrape_jobs import scrape_manager
    return scrape_manager.stats()


@router.get("/jobs/debug/freshness")
def debug_freshness(
    q: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    remote_only: Optional[bool] = Query(False),
    country: Optional[str] = Query(None),
    job_type: Optional[str] = Query(None),
    date_posted: Optional[str] = Query(None),
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Explains, per job, EXACTLY why it passed or failed the date filter for a
    given request. Run this with the same query params the UI sent and it will
    show the stored posted_date, scraped_at and fetched_at, which one the
    filter used, the computed age in hours, and the window it was compared
    against.

    Use this when a result looks too old: it turns "why is this here" from a
    guess into a line of output.
    """
    from pipeline.filter_lock import parse_bool
    from pipeline.freshness import effective_freshness, is_fresh_enough

    r_bool = parse_bool(remote_only)
    window = resolve_cutoff_minutes(date_posted)
    now = datetime.utcnow()

    rows = _build_job_query(
        db, title, platform, country, r_bool, job_type, date_posted, q, "hybrid"
    ).order_by(Job.posted_date.desc().nullslast()).limit(limit).all()

    out = []
    for j in rows:
        d = _serialize_job(j)
        ts, precision, source = effective_freshness(d)
        ok, why = is_fresh_enough(d, window, now)
        out.append({
            "title": d.get("title"),
            "platform": d.get("source_platform"),
            "stored_posted_date": d.get("posted_date"),
            "stored_scraped_at": d.get("scraped_at"),
            "stored_fetched_at": d.get("fetched_at"),
            "precision": precision,
            "filter_used_field": source,
            "age_hours": round((now - ts).total_seconds() / 3600, 2) if ts else None,
            "passes": ok,
            "reason": why,
        })

    stale = [o for o in out if not o["passes"]]
    return {
        "requested_window": date_posted or "(default)",
        "window_minutes": window,
        "window_hours": round(window / 60, 1),
        "server_utc_now": now.isoformat(),
        "rows_returned_by_sql": len(out),
        "would_be_blocked_by_guard": len(stale),
        "oldest_passing_hours": max(
            [o["age_hours"] for o in out if o["passes"] and o["age_hours"] is not None] or [0]
        ),
        "jobs": out,
    }


@router.get("/cache-stats")
def get_cache_stats(db: Session = Depends(get_db)):
    """How many paid scrapes the filter-aware cache has avoided."""
    return sc.get_cache_stats(db)


# ---------------------------------------------------------------------------
# PART 4 -- PER-PORTAL LIVE FETCH
# ---------------------------------------------------------------------------
@router.post("/portals/{portal_id}/live-fetch")
def run_portal_live_fetch(
    portal_id: str,
    q: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    remote_only: Optional[bool] = Query(False),
    job_type: Optional[str] = Query(None),
    date_posted: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Immediate, SYNCHRONOUS scrape scoped to ONE portal, using whatever
    filters are currently active in the dashboard.

    Deliberately synchronous (not a BackgroundTask): the UI needs the real
    outcome of THIS fetch to update the portal's status badge. Firing and
    forgetting would leave the badge showing a stale status, which is the
    reported bug.

    Bypasses the SearchCache serve-window on purpose -- this is an explicit
    manual "go get it now" action, not an incidental search. It still writes
    a SearchCache entry so a normal search right afterwards does not
    immediately re-scrape the same thing.
    """
    from pipeline.filter_lock import parse_bool
    from pipeline.date_filters import get_cache_policy
    from pipeline.five_tier_orchestrator import run_five_tier_orchestrator

    pid = (portal_id or "").strip().lower()
    known = {p.get("id") for p in load_portals_config()}
    if pid not in known:
        raise HTTPException(status_code=404, detail=f"Unknown portal '{portal_id}'")

    r_bool = parse_bool(remote_only)

    terms = []
    if title and title.strip().lower() != "all":
        terms.extend([t.strip() for t in title.split(",") if t.strip()])
    if q and q.strip():
        terms.append(q.strip())
    term_groups = parse_search_terms(terms)
    live_keyword = build_combined_scrape_keyword(term_groups)

    live_country = (country or "in").strip().lower()
    if live_country in ["all", ""]:
        live_country = "in"

    scope_hours = get_cache_policy(date_posted).get("native_recency_hours")

    filter_dict = sc.build_filter_dict(
        title=title, q=q, platform=pid, country=country,
        remote_only=r_bool, job_type=job_type, date_posted=date_posted,
    )
    cache_key = sc.compute_cache_key(filter_dict)

    if not sc.scrape_registry.try_acquire(cache_key):
        return {
            "status": "ALREADY_RUNNING",
            "portal_id": pid,
            "message": "A scrape for this portal and filter combination is already in flight.",
        }

    before = db.query(Job).filter(Job.source_platform == pid).count()
    result, error = None, None
    try:
        result = run_five_tier_orchestrator(
            keyword=live_keyword, country=live_country,
            remote_only=bool(r_bool), portals=[pid], since_hours=scope_hours,
        )
    except Exception as e:
        error = str(e)
        logger.error(f"[Portal Live Fetch] '{pid}' failed: {e}")
    finally:
        sc.scrape_registry.release(cache_key)

    db.expire_all()
    after = db.query(Job).filter(Job.source_platform == pid).count()
    raw_found = int((result or {}).get("layer_counts", {}).get(pid, 0))
    inserted = after - before

    # Record THIS attempt so the status badge reflects the manual trigger,
    # not just scheduled background runs. Success means the portal actually
    # returned rows -- inserting zero because everything was a duplicate is
    # still a working portal, so raw_found is what counts.
    try:
        db.add(RunLog(
            portal=pid,
            timestamp=datetime.now(timezone.utc),
            layer_used="Manual Live Fetch",
            success=bool(raw_found > 0),
            num_jobs_found=raw_found,
            error_message=error or (None if raw_found > 0 else
                                    "Portal returned 0 results for this query"),
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[Portal Live Fetch] Could not write RunLog for '{pid}': {e}")

    if not error:
        sc.record_scrape(db, cache_key, filter_dict,
                         sc.resolve_date_bucket(date_posted), "full")

    return {
        "status": "SUCCESS" if raw_found > 0 else ("ERROR" if error else "ZERO_RESULTS"),
        "portal_id": pid,
        "keyword": live_keyword,
        "country": live_country,
        "since_hours": scope_hours,
        "raw_jobs_found": raw_found,
        "new_jobs_inserted": max(0, inserted),
        "total_jobs_for_portal": after,
        "runtime_status": "ACTIVE" if raw_found > 0 else "DEACTIVATED",
        "error": error,
        "message": (
            f"Fetched {raw_found} listing(s) from {pid}; {max(0, inserted)} were new."
            if raw_found > 0 else
            (f"Live fetch failed: {error}" if error else
             f"{pid} returned 0 results for '{live_keyword}'. See /api/portals/diagnostics.")
        ),
    }


@router.post("/portals/refresh-all")
def refresh_all_portals(
    q: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    remote_only: Optional[bool] = Query(False),
    job_type: Optional[str] = Query(None),
    date_posted: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Force a live scrape across ALL TEN portals for the current filters, and
    report exactly what each one returned.

    This is the answer to "are all portals actually working?". A normal search
    already fans out to all ten, but it may be served from cache, so a portal
    contributing zero is ambiguous. This bypasses the cache serve-window and
    gives a per-portal verdict from a scrape that definitely just ran.
    """
    from pipeline.filter_lock import parse_bool
    from pipeline.date_filters import get_cache_policy
    from pipeline.five_tier_orchestrator import run_five_tier_orchestrator

    r_bool = parse_bool(remote_only)
    terms = []
    if title and title.strip().lower() != "all":
        terms.extend([t.strip() for t in title.split(",") if t.strip()])
    if q and q.strip():
        terms.append(q.strip())
    term_groups = parse_search_terms(terms)
    live_keyword = build_combined_scrape_keyword(term_groups)

    live_country = (country or "in").strip().lower()
    if live_country in ["all", ""]:
        live_country = "in"

    scope_hours = get_cache_policy(date_posted).get("native_recency_hours")
    all_portals = [p.get("id") for p in load_portals_config()]

    before = {
        pid: db.query(Job).filter(Job.source_platform == pid).count()
        for pid in all_portals
    }

    result, error = None, None
    try:
        result = run_five_tier_orchestrator(
            keyword=live_keyword, country=live_country,
            remote_only=bool(r_bool), portals=all_portals,
            since_hours=scope_hours,
        )
    except Exception as e:
        error = str(e)
        logger.error(f"[Refresh All] Failed: {e}")

    db.expire_all()
    raw = (result or {}).get("layer_counts", {}) or {}

    per_portal = []
    for pid in all_portals:
        after = db.query(Job).filter(Job.source_platform == pid).count()
        found = int(raw.get(pid, 0))
        # Write a RunLog so the Platforms view reflects this attempt, exactly
        # as the single-portal live fetch does.
        try:
            db.add(RunLog(
                portal=pid, timestamp=datetime.now(timezone.utc),
                layer_used="Refresh All", success=bool(found > 0),
                num_jobs_found=found,
                error_message=None if found > 0 else "Returned 0 results for this query",
            ))
        except Exception:
            pass
        per_portal.append({
            "portal_id": pid,
            "raw_jobs_found": found,
            "new_jobs_inserted": max(0, after - before[pid]),
            "total_in_db": after,
            "status": "ACTIVE" if found > 0 else "ZERO_RESULTS",
        })
    try:
        db.commit()
    except Exception:
        db.rollback()

    working = [p["portal_id"] for p in per_portal if p["raw_jobs_found"] > 0]
    silent = [p["portal_id"] for p in per_portal if p["raw_jobs_found"] == 0]

    return {
        "status": "ERROR" if error else "SUCCESS",
        "error": error,
        "keyword": live_keyword,
        "country": live_country,
        "since_hours": scope_hours,
        "portals_attempted": len(all_portals),
        "portals_returning_jobs": len(working),
        "working": working,
        "silent": silent,
        "per_portal": per_portal,
        "message": (
            f"{len(working)} of {len(all_portals)} portals returned jobs for "
            f"'{live_keyword}'."
            + (f" Silent: {', '.join(silent)}. Check /api/portals/diagnostics for why."
               if silent else "")
        ),
    }


@router.get("/portals/diagnostics")
def get_portal_diagnostics(db: Session = Depends(get_db)):
    """
    PART 4.1 -- per-portal failure diagnosis. For every portal, reports its
    most recent RunLog outcome and classifies WHY it is inactive, so a
    missing API key is never confused with a connector bug.
    """
    import os as _os

    # Which env credential each portal ultimately depends on.
    CREDENTIAL_REQUIREMENTS = {
        "linkedin": ["SERPAPI_API_KEY", "APIFY_API_TOKEN"],
        "indeed": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "glassdoor": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "dice": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "ziprecruiter": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "usajobs": ["USAJOBS_API_KEY", "USAJOBS_EMAIL"],
        "careerbuilder": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "simplyhired": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
        "weworkremotely": [],  # public RSS, no credential needed
        "hired": ["SERPAPI_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"],
    }

    def _has_any(keys):
        if not keys:
            return True
        for k in keys:
            if _os.getenv(k) or _os.getenv(f"{k}_1"):
                return True
        return False

    out = []
    for p in load_portals_config():
        pid = p.get("id")
        last = (db.query(RunLog).filter(RunLog.portal == pid)
                .order_by(RunLog.timestamp.desc()).first())
        required = CREDENTIAL_REQUIREMENTS.get(pid, [])
        creds_ok = _has_any(required)
        missing = [k for k in required if not (_os.getenv(k) or _os.getenv(f"{k}_1"))]

        err = (last.error_message or "") if last else ""
        err_l = err.lower()

        # A portal declared as needing a credential the user must apply for
        # (ZipRecruiter Partner API, CareerBuilder API) is ALWAYS user-action,
        # regardless of what its last RunLog said -- routing it to SerpApi is
        # a latency workaround, not a substitute for the real key.
        from pipeline.capabilities import portals_needing_api_key
        _needs_key = portals_needing_api_key().get(pid)
        if _needs_key and not (_os.getenv(_needs_key) or _os.getenv(f"{_needs_key}_1")):
            cause, fixable = "NEEDS_PARTNER_API_KEY", False
            missing = list(dict.fromkeys(missing + [_needs_key]))
        elif not creds_ok:
            cause, fixable = "MISSING_CREDENTIALS", False
        elif any(w in err_l for w in ["quota", "402", "payment", "credit", "insufficient", "429", "rate limit"]):
            cause, fixable = "QUOTA_OR_CREDIT_EXHAUSTED", False
        elif any(w in err_l for w in ["403", "forbidden", "allowlist", "blocked"]):
            cause, fixable = "BLOCKED_OR_NETWORK_DENIED", False
        elif "no module named" in err_l:
            cause, fixable = "MISSING_PYTHON_DEPENDENCY", True
        elif last is None:
            cause, fixable = "NEVER_RUN", True
        elif last.success:
            cause, fixable = "HEALTHY", True
        else:
            cause, fixable = "ZERO_RESULTS_OR_CONNECTOR_ISSUE", True

        out.append({
            "portal_id": pid,
            "portal_name": p.get("name"),
            "last_attempt": last.timestamp.isoformat() if last and last.timestamp else None,
            "last_layer": last.layer_used if last else None,
            "last_success": bool(last.success) if last else None,
            "last_jobs_found": (last.num_jobs_found if last else 0),
            "last_error": err or None,
            "diagnosed_cause": cause,
            "fixable_in_code": fixable,
            "required_credentials": required + ([_needs_key] if _needs_key else []),
            "routed_direct_to_serpapi": bool(_needs_key),
            "missing_credentials": missing,
            "jobs_in_db": db.query(Job).filter(Job.source_platform == pid).count(),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portals": out,
        "needs_user_action": [
            o["portal_id"] for o in out if not o["fixable_in_code"]
        ],
    }


@router.get("/stats")
def get_job_stats(db: Session = Depends(get_db)):
    total_jobs = db.query(Job).count()
    remote_jobs = db.query(Job).filter(Job.remote_flag == True).count()  # noqa: E712
    platform_counts = db.query(Job.source_platform, func.count(Job.id)).group_by(Job.source_platform).all()
    top_companies = (
        db.query(Job.company, func.count(Job.id)).group_by(Job.company).order_by(func.count(Job.id).desc()).limit(5).all()
    )
    pipeline_metrics = get_last_pipeline_metrics()
    return {
        "total_jobs": total_jobs,
        "remote_jobs": remote_jobs,
        "remote_percentage": round((remote_jobs / total_jobs * 100), 1) if total_jobs > 0 else 0,
        "platforms": {platform: count for platform, count in platform_counts},
        "top_companies": [{"company": comp, "count": count} for comp, count in top_companies],
        "last_run": pipeline_metrics,
        "dedup_ratio": f"{pipeline_metrics.get('dedup_ratio_pct', 0.0)}% Filtered ({pipeline_metrics.get('clean_ratio_pct', 100.0)}% Unique)",
    }


@router.get("/jobs/{job_id}")
def get_job_detail(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job with ID '{job_id}' not found")
    return _serialize_job(job)


@router.get("/status")
def get_pipeline_health_status(db: Session = Depends(get_db)):
    portals = load_portals_config()
    status_summary = []

    for p in portals:
        pid = p.get("id")
        pname = p.get("name")
        ptype = p.get("type")

        last_log = db.query(RunLog).filter(RunLog.portal == pid).order_by(RunLog.timestamp.desc()).first()
        job_count = db.query(Job).filter(Job.source_platform == pid).count()

        # AUDIT FIX [FIX-4]: naive cutoff for the naive posted_date column,
        # aware cutoff for the tz-aware fetched_at/scraped_at columns.
        _cut_naive = datetime.utcnow() - timedelta(days=7)
        _cut_aware = datetime.now(timezone.utc) - timedelta(days=7)
        visible_job_count = db.query(Job).filter(
            Job.source_platform == pid,
            or_(
                Job.posted_date >= _cut_naive,
                and_(Job.posted_date.is_(None), Job.fetched_at >= _cut_aware),
                and_(Job.posted_date.is_(None), Job.scraped_at >= _cut_aware),
            ),
        ).count()

        if last_log:
            # PART 4.4: status reflects the TRUE most-recent attempt for this
            # portal, whatever triggered it. Because /portals/{id}/live-fetch
            # writes its own RunLog row synchronously before returning, a
            # manual fetch flips this badge immediately -- previously only
            # scheduled background runs ever moved it.
            is_active = bool(last_log.success and (last_log.num_jobs_found or 0) > 0)
            status_summary.append({
                "portal_id": pid, "portal_name": pname, "type": ptype,
                "operating_layer": last_log.layer_used,
                "status": "HEALTHY" if last_log.success else "DEGRADED_CACHED",
                "runtime_status": "ACTIVE" if is_active else "DEACTIVATED",
                "last_run": last_log.timestamp.isoformat() if last_log.timestamp else None,
                "last_jobs_found": last_log.num_jobs_found,
                "total_jobs_in_db": job_count,
                "visible_jobs_30d": visible_job_count,
                "last_error": last_log.error_message,
                "tos_requires_api": p.get("tos_requires_api", False),
            })
        else:
            status_summary.append({
                "portal_id": pid, "portal_name": pname, "type": ptype,
                "operating_layer": None, "status": "UNKNOWN", "runtime_status": "DEACTIVATED",
                "last_run": None, "last_jobs_found": 0, "total_jobs_in_db": job_count,
                "visible_jobs_30d": visible_job_count, "last_error": None,
                "tos_requires_api": p.get("tos_requires_api", False),
            })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_configured_portals": len(portals),
        "portals_status": status_summary,
    }


@router.post("/pipeline/fallback-run")
def trigger_4layer_fallback_pipeline(
    portals: Optional[str] = Query(None),
    keyword: str = Query("developer"),
    country: str = Query("us"),
    background_tasks: BackgroundTasks = None,
):
    target_list = [p.strip() for p in portals.split(",") if p.strip()] if portals else None

    def run_fallback_task():
        try:
            run_4layer_pipeline(target_portals=target_list, keyword=keyword, country=country)
        except Exception as e:
            logger.error(f"Error executing 4-layer fallback pipeline: {e}")

    if background_tasks:
        background_tasks.add_task(run_fallback_task)
        return {"status": "ACCEPTED", "message": "4-Layer Fallback Pipeline triggered in background."}
    results = run_4layer_pipeline(target_portals=target_list, keyword=keyword, country=country)
    return {"status": "SUCCESS", "results": results}


@router.post("/pipeline/five-tier-run")
def trigger_5tier_orchestrator_pipeline(
    portals: Optional[str] = Query(None),
    keyword: str = Query("developer"),
    country: str = Query("in"),
    remote_only: bool = Query(False),
    since_hours: Optional[int] = Query(None, description="Scope the scrape to only the last N hours"),
    background_tasks: BackgroundTasks = None,
):
    from pipeline.five_tier_orchestrator import run_five_tier_orchestrator
    target_list = [p.strip() for p in portals.split(",") if p.strip()] if portals else None

    def run_five_tier_task():
        try:
            run_five_tier_orchestrator(
                keyword=keyword, country=country, remote_only=remote_only,
                portals=target_list, since_hours=since_hours,
            )
        except Exception as e:
            logger.error(f"Error executing 5-tier orchestrator pipeline: {e}")

    if background_tasks:
        background_tasks.add_task(run_five_tier_task)
        return {"status": "ACCEPTED", "message": "5-Tier Orchestrator Pipeline triggered in background."}
    results = run_five_tier_orchestrator(
        keyword=keyword, country=country, remote_only=remote_only,
        portals=target_list, since_hours=since_hours,
    )
    return {"status": "SUCCESS", "results": results}


@router.post("/pipeline/max-coverage-run")
def trigger_max_coverage_pipeline(
    portals: Optional[str] = Query(None),
    keyword: str = Query("developer"),
    country: str = Query("in"),
    background_tasks: BackgroundTasks = None,
):
    from pipeline.max_coverage_orchestrator import run_max_coverage
    target_list = [p.strip() for p in portals.split(",") if p.strip()] if portals else None

    def run_max_coverage_task():
        try:
            run_max_coverage(keyword=keyword, country=country, portals=target_list)
        except Exception as e:
            logger.error(f"Error executing Max-Coverage Waterfall pipeline: {e}")

    if background_tasks:
        background_tasks.add_task(run_max_coverage_task)
        return {"status": "ACCEPTED", "message": "Max-Coverage 5-Method Waterfall Pipeline triggered in background."}
    results = run_max_coverage(keyword=keyword, country=country, portals=target_list)
    return {"status": "SUCCESS", "results": results}


@router.get("/pipeline/method-health")
def get_method_health_dashboard():
    from pipeline.method_health import MethodHealthMonitor
    monitor = MethodHealthMonitor()
    return {"status": "SUCCESS", "reports": monitor.get_all_health_reports()}
