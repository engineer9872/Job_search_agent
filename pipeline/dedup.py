import logging
import re
from typing import List, Dict, Set, Tuple, Any, Optional
from rapidfuzz import fuzz
from pipeline.normalize import NormalizedJob

logger = logging.getLogger(__name__)

COMMON_ABBREVIATIONS = {
    r"\bsr\b": "senior",
    r"\bjr\b": "junior",
    r"\bdev\b": "developer",
    r"\beng\b": "engineer",
    r"\bmgr\b": "manager",
    r"\binc\b": "",
    r"\bcorp\b": "",
    r"\bllc\b": "",
    r"\bltd\b": "",
    r"\bco\b": "",
}


def clean_str_for_matching(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    for pattern, replacement in COMMON_ABBREVIATIONS.items():
        cleaned = re.sub(pattern, replacement, cleaned)
    return " ".join(cleaned.split())


def build_job_parts(title: str, company: str, city: str = "") -> Tuple[str, str, str]:
    return (
        clean_str_for_matching(title),
        clean_str_for_matching(company),
        clean_str_for_matching(city),
    )


def build_job_signature(job: NormalizedJob) -> str:
    """
    Kept for any external callers wanting a display-friendly composite
    string (logging etc). Match decisions no longer use this.
    """
    t, c, ci = build_job_parts(job.title, job.company, job.city or "")
    return f"{t} {c} {ci}".strip()


class Deduplicator:
    """
    Company match is a hard gate: two jobs are never flagged duplicate
    on title similarity alone across different companies.
    """

    def __init__(self, title_threshold: float = 88.0, company_threshold: float = 90.0, similarity_threshold: float = None):
        # similarity_threshold kept as accepted kwarg for backward compat with
        # existing call site (Deduplicator(similarity_threshold=88.0)) --
        # maps onto title_threshold so that call site keeps working unchanged.
        self.title_threshold = similarity_threshold if similarity_threshold is not None else title_threshold
        self.company_threshold = company_threshold

    def is_fuzzy_match(self, parts1: Tuple[str, str, str], parts2: Tuple[str, str, str]) -> bool:
        title1, company1, _ = parts1
        title2, company2, _ = parts2

        if not company1 or not company2:
            return False

        if fuzz.token_set_ratio(company1, company2) < self.company_threshold:
            return False

        return fuzz.token_set_ratio(title1, title2) >= self.title_threshold

    def deduplicate(
        self,
        new_jobs: List[NormalizedJob],
        existing_signatures: Optional[List[Tuple[str, str, str, str]]] = None,
    ) -> Tuple[List[NormalizedJob], int]:
        seen_urls: Set[str] = set()
        seen_parts: List[Tuple[str, str, str]] = []
        # PART 4: parts -> the apply_url of the existing DB row they came from.
        # Without this we could detect a fuzzy duplicate but had no way to say
        # WHICH stored row it duplicated, so the only option was to drop the
        # incoming job. That silently discarded genuinely newer reposts --
        # 70-90% of every scrape was being thrown away as "duplicate".
        parts_to_url: dict = {}
        # Fuzzy matches that may be FRESHER than what we have stored. These are
        # not returned as new inserts; the caller compares dates and upserts
        # the existing row's freshness fields.
        self.refresh_candidates: List[Tuple[Any, str]] = []

        if existing_signatures:
            for row in existing_signatures:
                url, title, company, city = row
                # NOTE: intentionally NOT pre-seeding seen_urls with existing
                # DB apply_urls here. A newly-scraped job whose apply_url
                # exactly matches an existing DB row must NOT be silently
                # dropped as a duplicate -- it needs to reach the insert
                # loop, which treats an apply_url match as an UPSERT
                # candidate (refreshes posted_date/fetched_at when the
                # source now reports newer info, e.g. a repost). Only fuzzy
                # title+company+location matches (different apply_url,
                # can't be safely attributed to one existing row to update)
                # are still pre-seeded below.
                _p = build_job_parts(title, company, city)
                seen_parts.append(_p)
                parts_to_url.setdefault(_p, url)

        unique_jobs: List[NormalizedJob] = []
        duplicates_count = 0

        for job in new_jobs:
            url = job.apply_url.strip()

            if url in seen_urls:
                logger.debug(f"Skipping exact URL duplicate: {url}")
                duplicates_count += 1
                continue

            parts = build_job_parts(job.title, job.company, job.city or "")
            is_dup = False

            for existing_parts in seen_parts:
                if self.is_fuzzy_match(parts, existing_parts):
                    matched_url = parts_to_url.get(existing_parts)
                    if matched_url:
                        # Do NOT discard outright. Hand it to the caller with
                        # the row it matched, so a genuinely newer repost can
                        # refresh that row's posted_date/fetched_at instead of
                        # vanishing.
                        self.refresh_candidates.append((job, matched_url))
                    logger.debug(
                        f"Fuzzy duplicate (company+title match):\n"
                        f"  New:      '{job.title}' @ '{job.company}'\n"
                        f"  Matched:  '{existing_parts[0]}' @ '{existing_parts[1]}'"
                    )
                    is_dup = True
                    break

            if is_dup:
                duplicates_count += 1
                continue

            seen_urls.add(url)
            seen_parts.append(parts)
            unique_jobs.append(job)

        logger.info(
            f"Deduplication complete. Retained {len(unique_jobs)} unique jobs, "
            f"{duplicates_count} fuzzy duplicates ({len(self.refresh_candidates)} of which "
            f"are attributable to a stored row and will be date-checked for refresh)."
        )
        return unique_jobs, duplicates_count
