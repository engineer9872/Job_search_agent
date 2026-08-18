"""
Multi-source racing fetcher.

Instead of trying sources sequentially (Firecrawl -> wait -> fallback ->
wait -> fallback...), this runs Firecrawl AND each portal's existing
connector (which already has its own internal fast-tier logic: SerpApi /
Apify / RSS) CONCURRENTLY, and returns whichever produces real, non-empty
results first within a short "fast" window.

Firecrawl stays authoritative: if it hasn't finished by the time a faster
source responds, it keeps running in the background and its results are
normalized + written into the DB when done, so the NEXT request for that
portal benefits from fresher/richer Firecrawl data via the T5 cache path —
without making the current user wait on it.
"""

import logging
import concurrent.futures
from typing import List, Dict, Any, Tuple, Optional

from connectors.firecrawl_client import fetch_jobs_via_firecrawl

logger = logging.getLogger(__name__)

import os

# Emergency kill-switch. Set FREE_TIER_DISABLED=1 to fall straight back to the
# old paid-first behaviour without a redeploy.
_DISABLE_FREE_TIER = os.getenv("FREE_TIER_DISABLED", "").strip() in ("1", "true", "yes")

# Free-tier jobs that were not sufficient ON THEIR OWN are kept here and merged
# with the paid results, so a portal that yields 3 free matches plus 4 SerpApi
# matches returns all 7 rather than discarding the free 3.
_FREE_TIER_PARTIAL: Dict[str, List[Dict[str, Any]]] = {}


def _merge_partial(portal_id: str, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    partial = _FREE_TIER_PARTIAL.pop(portal_id, None)
    if not partial:
        return jobs
    seen = {str(j.get("url") or j.get("apply_url") or "") for j in jobs}
    merged = list(jobs)
    for j in partial:
        u = str(j.get("url") or j.get("apply_url") or "")
        if u and u not in seen:
            merged.append(j)
            seen.add(u)
    logger.info(
        f"[Race:{portal_id}] Merged {len(merged) - len(jobs)} free-tier job(s) "
        f"into the paid result set."
    )
    return merged

# How long we wait for ANY source before returning to the caller.
# Firecrawl (~46s observed) will usually lose this race to SerpApi/RSS/Apify
# (typically 1-10s) — that's intentional, it's what makes this fast.
FAST_WINDOW_SECONDS = 12.0

# Absolute ceiling if literally nothing has returned by FAST_WINDOW_SECONDS —
# we wait a bit longer rather than give up completely.
HARD_CEILING_SECONDS = 30.0


def _run_firecrawl(portal_id: str, keyword: str, location: str,
                   bypass_budget: bool = False) -> List[Dict[str, Any]]:
    try:
        return fetch_jobs_via_firecrawl(portal_id, keyword=keyword, location=location,
                                        bypass_budget=bypass_budget)
    except Exception as e:
        logger.warning(f"[Race:{portal_id}] Firecrawl branch failed: {e}")
        return []


def _run_existing_connector(portal_id: str, connector_fetch_fn, **kwargs) -> List[Dict[str, Any]]:
    try:
        return connector_fetch_fn(**kwargs) or []
    except Exception as e:
        logger.warning(f"[Race:{portal_id}] Existing-connector branch failed: {e}")
        return []


def _background_firecrawl_cache_refresh(
    future: concurrent.futures.Future,
    portal_id: str,
    country: str,
):
    """
    Runs in a background thread after we've already responded to the user.
    When Firecrawl's future eventually completes, normalize + insert its
    results into the DB so subsequent requests get fresher data via cache.
    """
    try:
        jobs = future.result(timeout=HARD_CEILING_SECONDS)
    except Exception as e:
        logger.info(f"[Race:{portal_id}] Background Firecrawl completion failed/timed out: {e}")
        return

    if not jobs:
        return

    try:
        from DB import SessionLocal, Job
        from pipeline.normalize import normalize_job_batch

        normalized = normalize_job_batch(raw_jobs=jobs, source_platform=portal_id, country=country)
        db = SessionLocal()
        inserted = 0
        try:
            for norm_job in normalized:
                existing = db.query(Job).filter(Job.apply_url == norm_job.apply_url).first()
                if not existing:
                    db.add(Job(**norm_job.to_dict()))
                    db.commit()
                    inserted += 1
        finally:
            db.close()
        logger.info(
            f"[Race:{portal_id}] Background Firecrawl completed with {len(jobs)} jobs "
            f"({inserted} new records written to cache for next request)."
        )
    except Exception as e:
        logger.warning(f"[Race:{portal_id}] Background cache write failed: {e}")


def fetch_portal_race(
    portal_id: str,
    connector_fetch_fn,
    connector_kwargs: Dict[str, Any],
    keyword: str = "developer",
    country: str = "us",
    location: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Races Firecrawl against the portal's existing connector logic.
    Returns (jobs, source_used) using whichever responds first with
    non-empty results within FAST_WINDOW_SECONDS. If neither has responded
    by then, waits up to HARD_CEILING_SECONDS for whichever finishes.

    connector_fetch_fn: the existing connector's bound .fetch_jobs method
    connector_kwargs: kwargs to call it with (keyword, country, remote_only, etc.)

    TIER 0 GATE (see pipeline/escalation.py):
    Before ANY paid provider is touched, the free tier runs -- the portal's own
    RSS/API, then schema.org JSON-LD embedded in its search page, then static
    HTML cards. If that already yields enough EXACT MATCHES for the user's
    query, this function returns immediately and no SerpApi call, no Apify run
    and no Firecrawl credit is ever spent for this portal.

    The gate measures exact matches, not row count: 40 rows of
    "Cloud Sales Executive" for a "cloud engineer" query is a failed search and
    correctly escalates, while 6 genuine Cloud Engineer roles correctly stops.
    """
    # ---- TIER 0: FREE ----------------------------------------------------
    _tier0_blocked = False
    _tier0_block_reason = None
    if not _DISABLE_FREE_TIER:
        try:
            from pipeline.free_tier import fetch_free_tier
            from pipeline.escalation import should_escalate

            since_hours = connector_kwargs.get("since_hours")
            t0 = fetch_free_tier(portal_id, keyword, location or country, since_hours)

            if t0.jobs:
                escalate, reason, metrics = should_escalate(t0.jobs, keyword, "free")
                if not escalate:
                    logger.info(
                        f"[Race:{portal_id}] ANSWERED BY FREE TIER via {t0.method} -- "
                        f"{len(t0.jobs)} jobs, {metrics['exact_matches']} exact matches "
                        f"(yield {metrics['match_yield']}). ZERO paid calls made."
                    )
                    return t0.jobs, f"tier0_free_{t0.method}"

                logger.info(
                    f"[Race:{portal_id}] Free tier returned {len(t0.jobs)} jobs but only "
                    f"{metrics['exact_matches']} exact matches ({reason}) -- escalating to paid."
                )
                _FREE_TIER_PARTIAL[portal_id] = t0.jobs
            else:
                if t0.blocked or t0.js_shell:
                    _tier0_blocked = True
                    _tier0_block_reason = t0.blocked or "js_only_shell"
                logger.info(
                    f"[Race:{portal_id}] Free tier empty "
                    f"({t0.blocked or t0.method}) -- escalating to paid."
                )
        except Exception as e:
            logger.warning(f"[Race:{portal_id}] Free tier errored, escalating: {e}")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    # Do not even enter the race with Firecrawl for portals that reject it
    # (LinkedIn) or that we have deliberately routed straight to SerpApi
    # (ZipRecruiter, CareerBuilder). Previously every portal submitted a
    # Firecrawl branch unconditionally, so a guaranteed-failing LinkedIn call
    # ran on every single search.
    from pipeline.capabilities import firecrawl_supported
    _use_firecrawl = firecrawl_supported(portal_id)

    # ESCALATE A BLOCKED FREE TIER STRAIGHT TO FIRECRAWL.
    #
    # When Tier 0 comes back `blocked` (403, Cloudflare, DataDome, a JS-only
    # shell) that is not "no jobs" -- it is "this page needs a real browser
    # with an anti-bot proxy". That is precisely what Firecrawl's stealth tier
    # is for, and it is the one provider that can actually get the page.
    #
    # So a blocked portal gets Firecrawl even when the budget would otherwise
    # have deprioritised it: spending a Firecrawl credit on a page nothing else
    # can reach is the single highest-value call we can make. A portal whose
    # free tier merely returned nothing (not blocked) does NOT get this
    # treatment -- SerpApi is cheaper and works fine there.
    if _tier0_blocked and firecrawl_supported(portal_id):
        _use_firecrawl = True
        logger.info(
            f"[Race:{portal_id}] Tier 0 was blocked ({_tier0_block_reason}). "
            f"Prioritising Firecrawl -- its stealth proxy is the only source that "
            f"can reach a bot-walled page."
        )

    firecrawl_future = (
        executor.submit(_run_firecrawl, portal_id, keyword, location or country,
                        _tier0_blocked)
        if _use_firecrawl else None
    )
    existing_future = executor.submit(_run_existing_connector, portal_id, connector_fetch_fn, **connector_kwargs)

    pending = {existing_future: "existing_tiers"}
    if firecrawl_future is not None:
        pending[firecrawl_future] = "firecrawl"
    else:
        logger.info(
            f"[Race:{portal_id}] Firecrawl branch skipped (portal rejects it or is "
            f"routed straight to SerpApi). Racing existing tiers only."
        )

    try:
        done, still_pending = concurrent.futures.wait(
            pending.keys(), timeout=FAST_WINDOW_SECONDS, return_when=concurrent.futures.FIRST_COMPLETED
        )

        # Check completed futures for the first one with REAL (non-empty) results
        for fut in done:
            source_name = pending[fut]
            try:
                result = fut.result()
            except Exception:
                result = []
            if result:
                logger.info(f"[Race:{portal_id}] '{source_name}' won the race with {len(result)} jobs (within {FAST_WINDOW_SECONDS}s window).")
                # If Firecrawl was the one that finished, no background work needed.
                # If the existing-tiers path won, let Firecrawl keep running in the
                # background to refresh the cache for next time.
                if source_name == "existing_tiers" and firecrawl_future in still_pending:
                    import threading
                    threading.Thread(
                        target=_background_firecrawl_cache_refresh,
                        args=(firecrawl_future, portal_id, country),
                        daemon=True,
                    ).start()
                else:
                    executor.shutdown(wait=False)
                return _merge_partial(portal_id, result), source_name

        # Both completed within the window but were empty, OR only one completed
        # and it was empty — wait a bit longer for whichever is still pending.
        if still_pending:
            done2, still_pending2 = concurrent.futures.wait(
                still_pending, timeout=(HARD_CEILING_SECONDS - FAST_WINDOW_SECONDS),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done2:
                source_name = pending[fut]
                try:
                    result = fut.result()
                except Exception:
                    result = []
                if result:
                    logger.info(f"[Race:{portal_id}] '{source_name}' returned {len(result)} jobs after extended wait.")
                    return _merge_partial(portal_id, result), source_name

        logger.info(f"[Race:{portal_id}] No source returned results within {HARD_CEILING_SECONDS}s.")
        # Even when every paid source fails, hand back whatever the free tier
        # managed to collect rather than returning nothing.
        leftover = _merge_partial(portal_id, [])
        return leftover, ("tier0_free_partial" if leftover else "none")

    finally:
        executor.shutdown(wait=False)
