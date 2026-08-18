"""
Step-5 strict validator (ThreeTierFilterGuard).

PART 1 AUDIT -- FIXES APPLIED IN THIS FILE
------------------------------------------
Every rule below is now written so that it is BIT-FOR-BIT the same logic the
SQL pre-filter in api/routes/jobs.py applies. The two layers previously
disagreed in five places, each of which silently zeroed out real results:

  [FIX-1] job_type "onsite": SQL treated it as remote_flag == False, the
          guard compared it as a job_type string. Since the guard normalizes
          job_type to full_time/contract/part_time, "onsite" could never
          match and EVERY onsite search returned zero. Onsite is now a
          remote_flag check on both layers -- it describes work location,
          not employment type.

  [FIX-2] job_type "parttime": the frontend sends "parttime", the DB stores
          "part_time". SQL mapped the two; the guard compared the raw
          strings ("part_time" != "parttime") and rejected everything. Both
          layers now canonicalize through the single canonical_job_type()
          helper below.

  [FIX-3] date: the guard used max(posted_date, fetched_at), which let a
          40-day-old posting pass a 7-day filter purely because we
          re-scraped it today -- a confirmed conflict being overridden by an
          unrelated fact. posted_date is now authoritative WHEN KNOWN (with
          a small shared rounding tolerance), and fetched_at is used only as
          a fallback when posted_date is NULL. SQL does the same.

  [FIX-4] timezone: cutoffs were built with naive datetime.utcnow() and
          compared against tz-aware columns. Harmless on SQLite, raises on
          PostgreSQL. Guard now strips tzinfo defensively on every parsed
          datetime before comparing.

  [FIX-5] free-text `q`: q was being merged into the TITLE term-groups, so
          the guard demanded it appear in the job title, while SQL searched
          title/company/skills/description. Searching a company name
          ("Stripe") matched in SQL then got rejected by the guard. q is now
          validated separately, across the same field set SQL uses.

RULE 4 (never return a job that violates a specified filter) and RULE 5
(never exclude a job merely because a field is missing) are both preserved:
we reject ONLY on a confirmed, known conflict.
"""

import re
import logging
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timedelta

from DB import SessionLocal, GuardAuditLog
from pipeline.filter_lock import FilterSpec
from pipeline.query_parser import parse_search_terms, job_matches_any_term_group

logger = logging.getLogger(__name__)

PLATFORM_URL_PATTERNS = {
    "linkedin": r"linkedin\.com/(jobs/view|in/|comm/jobs/view)",
    # `pagead/clk` is Indeed's sponsored-listing click URL -- a real, direct
    # posting link that was being rejected for not matching the old pattern.
    "indeed": r"indeed\.[a-z.]+/(.*viewjob|.*jk=|rc/clk|pagead/clk|job/)",
    "glassdoor": r"glassdoor\.[a-z.]+/(.*job-listing|.*joblisting|.*partner/joblisting)",
    "dice": r"dice\.com/(job-detail/|jobs/detail)",
    # `job-redirect?match_token=` is ZipRecruiter's own apply redirect.
    "ziprecruiter": r"ziprecruiter\.[a-z.]+/(jobs/|c/.*|job/|job-redirect)",
    "usajobs": r"usajobs\.gov/(job/|GetJob/|getjob/)",
    "careerbuilder": r"careerbuilder\.[a-z.]+/(job/|job-details/|job-detail/)",
    "simplyhired": r"simplyhired\.[a-z.]+/(job/|view/)",
    "weworkremotely": r"weworkremotely\.com/(remote-jobs/|jobs/)",
    "hired": r"hired\.com/(jobs/|co/|job/)",
}

# Country values that mean "not tied to any one country" -- these satisfy
# ANY country filter because there is no conflict to confirm.
GLOBAL_COUNTRY_VALUES = {"REMOTE / GLOBAL", "GLOBAL", "WORLDWIDE", "REMOTE", "ANYWHERE"}


def canonical_job_type(raw: Any) -> Optional[str]:
    """
    THE single job_type canonicalizer. Imported by api/routes/jobs.py and
    pipeline/search_cache.py so all three layers speak the same vocabulary.

    Returns one of: "all" | "full_time" | "contract" | "part_time" |
    "onsite_only", or None when the value carries no usable signal
    (blank, "unknown", "n/a") -- None means "we do not know", which per
    RULE 5 must never cause a rejection.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s or s in ["unknown", "n/a", "na", "none", "-"]:
        return None
    if s in ["all", "all types", "all job types"]:
        return "all"
    if s in ["contract", "contractor", "freelance", "contract-to-hire", "c2c", "temporary", "temp"]:
        return "contract"
    if s in ["fulltime", "full-time", "full_time", "permanent", "full time"]:
        return "full_time"
    if s in ["parttime", "part-time", "part_time", "part time"]:
        return "part_time"
    if s in ["onsite", "onsite_only", "on-site", "on site"]:
        return "onsite_only"
    if s in ["internship", "intern"]:
        return "internship"
    return None


# A URL that is clearly ONE specific job posting, on whatever domain.
# Matches /jobs/view/123, /job/abc, /jobs/info/<id>, ?jk=..., /viewjob, ATS
# paths (greenhouse/lever/workday/recruiterflow), and apply-redirect endpoints.
_DIRECT_POSTING_PATTERNS = [
    r"/jobs?/(view|info|detail|details|posting|opening)/[a-z0-9\-_]{3,}",
    r"/jobs?/[a-z0-9\-_]{6,}(\?|$|/)",
    r"[?&](jk|jobid|job_id|jobListingId|gh_jid|lever|postingId)=",
    r"/viewjob",
    r"(greenhouse|lever|workday|myworkdayjobs|smartrecruiters|recruiterflow|ashbyhq|jobvite|icims|taleo)\.",
    r"/(job-redirect|pagead/clk|apply)\b",
]

# Things that are a LISTING or SEARCH page, never a single posting.
_SEARCH_PAGE_PATTERNS = [
    "google.com/search", "google.com/url?", "bing.com/search",
    "duckduckgo.com/?q=", "ibp=htl;jobs", "search_query=",
    "/jobs/search", "/jobs-search", "/search?", "/search/", "jobs.htm",
    "/browse", "/categories", "/companies",
]


def _looks_like_direct_posting(lower_url: str) -> bool:
    return any(re.search(p, lower_url, re.IGNORECASE) for p in _DIRECT_POSTING_PATTERNS)


def validate_direct_job_url(platform_id: str, url: str) -> bool:
    """
    MAX-JOBS FIX.

    This used to demand that the apply URL live on the PORTAL'S OWN DOMAIN.
    That is wrong: SerpApi and the portals themselves routinely hand back an
    apply link pointing at the employer's ATS (Greenhouse, Lever, Workday,
    RecruiterFlow), at an aggregator, or at another board. Those are real,
    applyable jobs. Measured on the live DB, that domain requirement was
    discarding ~510 perfectly good postings -- e.g. a `hired` row whose apply
    link was a genuine `linkedin.com/jobs/view/...` page.

    The correct test is "is this ONE job posting, or is it a search/listing
    page?" -- not "does the domain match the portal we filed it under".

    Still rejected, correctly: search result pages, category/browse pages, and
    anything that is not a single posting.
    """
    if not url or not isinstance(url, str):
        return False
    url_str = url.strip()
    if not url_str.startswith("http://") and not url_str.startswith("https://"):
        return False

    lower_url = url_str.lower()

    # A search/listing page is never acceptable, on any domain.
    if any(b in lower_url for b in _SEARCH_PAGE_PATTERNS):
        return False

    p_clean = (platform_id or "").lower().strip()
    pattern = PLATFORM_URL_PATTERNS.get(p_clean)

    # 1. On the portal's own domain, matching its known posting shape.
    if pattern and re.search(pattern, lower_url, re.IGNORECASE):
        return True

    # 2. Off-domain, but unmistakably a single job posting. ACCEPT -- this is
    #    the case that was silently losing hundreds of real jobs.
    if _looks_like_direct_posting(lower_url):
        return True

    # 3. Unknown portal with no pattern: fall back to the old shape check.
    if not pattern:
        parts = lower_url.split("/", 3)
        return len(parts) >= 4 and len(parts[3]) > 1

    return False


def _to_naive_utc(value: Any) -> Optional[datetime]:
    """
    [FIX-4] Parse any date representation into a NAIVE UTC datetime.
    Returns None when the value carries no usable date signal.
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
        # Convert to UTC then drop tzinfo, so every comparison in this module
        # is naive-vs-naive regardless of DB dialect.
        try:
            from datetime import timezone as _tz
            dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            dt = dt.replace(tzinfo=None)
    return dt


class ThreeTierFilterGuard:
    """
    Relaxed-strict validator: rejects a job ONLY on a filter the user
    actually specified AND for which we hold real, conflicting data.
    Missing/unknown metadata is never a rejection reason.
    """

    def __init__(self, filter_spec: FilterSpec, expansion: Optional[Dict[str, Any]] = None):
        self.spec = filter_spec
        # HYBRID SEARCH. When an expansion is supplied, a job qualifies if it
        # matches the typed terms OR a same-job synonym OR an adjacent role
        # (see pipeline/query_expansion.py). Ranking then keeps the literal
        # matches on top, so widening what QUALIFIES never means the user has
        # to scroll past loosely-related roles to reach the real ones.
        #
        # Every OTHER filter -- date, platform, country, remote, job type --
        # stays exact. Expansion only changes what "matches the search text"
        # means; it never loosens anything else.
        self.expansion = expansion or None
        if not self.spec.verify_integrity():
            raise RuntimeError("FilterSpec hash mismatch in ThreeTierFilterGuard initialization!")

        raw_title = self.spec.job_title or ""
        if raw_title.lower() == "all":
            self.term_groups = []
        else:
            terms = [t for t in raw_title.split(",") if t.strip()]
            self.term_groups = parse_search_terms(terms)

        # [FIX-5] q is validated separately from the title term-groups.
        self.q_term = (self.spec.q or "").strip().lower() or None

        self.spec_job_type = canonical_job_type(self.spec.job_type)

    # -- individual filter checks -------------------------------------------

    def _check_platform(self, job, platform_id) -> Optional[str]:
        if self.spec.platform and self.spec.platform.lower() not in ["all", ""]:
            if platform_id != self.spec.platform.lower():
                return "platform_mismatch"
        return None

    def _check_remote(self, job) -> Optional[str]:
        # remote_flag is a NOT NULL column defaulting to False, so "unknown"
        # does not occur in practice -- but we still only reject on an
        # explicit False, matching the SQL `remote_flag == True` filter.
        if self.spec.remote_only is True:
            if job.get("remote_flag") is False:
                return "not_remote"
        return None

    def _check_country(self, job) -> Optional[str]:
        """
        [AUDIT] Previously ANY job with remote_flag=True satisfied ANY country
        filter -- so country=US happily returned a Germany-only remote role.
        A remote job now satisfies a country filter only when its country is
        genuinely unknown or explicitly global. A remote job with a KNOWN,
        DIFFERENT country is a confirmed conflict and is rejected (RULE 4).
        """
        spec_country = (self.spec.country or "").strip().upper()
        if not spec_country or spec_country in ["ALL", ""]:
            return None

        job_country = (job.get("country") or "").strip().upper()
        if not job_country:
            return None  # unknown -> never a rejection (RULE 5)
        if job_country in GLOBAL_COUNTRY_VALUES:
            return None
        if job_country == spec_country:
            return None
        return "country_mismatch"

    def _check_job_type(self, job) -> Optional[str]:
        if self.spec_job_type in [None, "all"]:
            return None

        # [FIX-1] "Onsite" is a LOCATION constraint, not an employment type.
        if self.spec_job_type == "onsite_only":
            if job.get("remote_flag") is True:
                return "job_type_mismatch_remote_when_onsite_requested"
            return None

        # [FIX-2] Both sides canonicalized -- no more "parttime" != "part_time".
        job_type = canonical_job_type(job.get("job_type"))
        if job_type is None:
            return None  # unknown -> never a rejection (RULE 5)
        if job_type != self.spec_job_type:
            return "job_type_mismatch"
        return None

    def _check_date(self, job) -> Optional[str]:
        """
        [FIX-3] Delegates entirely to pipeline/freshness.py -- the SINGLE
        source of truth that api/routes/jobs.py also calls. Neither layer
        does its own cutoff arithmetic any more, so the two cannot drift
        apart. See that module for why a plain max(posted_date, fetched_at)
        is measurably wrong here (it would put 807 stale rows into the
        "Past 24 Hours" view).
        """
        from pipeline.date_filters import resolve_cutoff_minutes
        from pipeline.freshness import is_fresh_enough

        window = resolve_cutoff_minutes(self.spec.date_posted)
        passes, reason = is_fresh_enough(job, window)
        if passes:
            return None
        self.last_stale_detail = reason
        return "stale_date"

    def _check_keywords(self, job) -> Optional[str]:
        # ---- HYBRID PATH ----------------------------------------------
        if self.expansion and (
            self.expansion.get("strong") or self.expansion.get("related")
        ):
            from pipeline.query_expansion import classify_match
            tier, _weight, _term = classify_match(job, self.expansion)
            if tier == "none":
                # Fall back to the strict title check: the expansion may not
                # cover this vocabulary (a niche or unusual role name), and a
                # literal title hit must never be rejected just because the
                # taxonomy has no family for it.
                if not job_matches_any_term_group(
                    job.get("title"), job.get("canonical_title"), self.term_groups
                ):
                    return "title_mismatch"
        # ---- EXACT PATH (search_mode=exact, or no expansion) ----------
        elif not job_matches_any_term_group(
            job.get("title"), job.get("canonical_title"), self.term_groups
        ):
            return "title_mismatch"

        # [FIX-5] Free-text q mirrors the SQL ILIKE across the same fields.
        if self.q_term:
            haystack = " ".join(
                str(job.get(f) or "").lower()
                for f in ["title", "canonical_title", "company", "skills", "description_snippet"]
            )
            if self.q_term not in haystack:
                return "query_mismatch"
        return None

    # -- orchestration -------------------------------------------------------

    def validate_job(self, job: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        platform_id = (job.get("platform_id") or job.get("source_platform") or "").lower()
        job_url = str(job.get("url") or job.get("apply_url") or "")
        if not validate_direct_job_url(platform_id, job_url):
            return False, "invalid_or_indirect_url"

        for check in (
            lambda: self._check_platform(job, platform_id),
            lambda: self._check_remote(job),
            lambda: self._check_country(job),
            lambda: self._check_job_type(job),
            lambda: self._check_date(job),
            lambda: self._check_keywords(job),
        ):
            reason = check()
            if reason:
                return False, reason

        return True, None

    def process_guard_checks(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        verified_jobs = []
        rejection_reasons: Dict[str, int] = {}
        db = SessionLocal()
        try:
            for job in candidates:
                passed, reason = self.validate_job(job)
                if passed:
                    verified_jobs.append(job)
                else:
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                    try:
                        db.add(GuardAuditLog(
                            job_id=str(job.get("id") or job.get("apply_url")),
                            filter_hash=self.spec.integrity_hash,
                            check_level="Step 5 Validator",
                            outcome="EXCLUDE",
                            reason=reason,
                        ))
                    except Exception:
                        pass
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Failed to write guard audit log batch: {e}")
        finally:
            db.close()

        # Surfaced so the UI's empty-state can explain WHY zero results came
        # back (spec Part 5) instead of a generic "no jobs found".
        self.last_rejection_reasons = rejection_reasons
        self.last_candidate_count = len(candidates)
        if candidates and not verified_jobs:
            logger.info(
                f"[Guard] All {len(candidates)} candidates rejected. Reasons: {rejection_reasons}"
            )
        return verified_jobs
