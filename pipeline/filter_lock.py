import hashlib
import json
from typing import Optional, List, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

# job_title is now FREE TEXT -- no fixed canonical list. Any string the user
# types or selects is accepted here; matching accuracy is enforced later by
# pipeline.query_parser's dynamic token matcher, not by validating against a
# hardcoded title list.

VALID_JOB_TYPES = ["all", "full_time", "fulltime", "contract", "contractor", "freelance", "part_time", "parttime", "onsite_only", "onsite"]

from pipeline.date_filters import VALID_DATE_FILTER_VALUES
VALID_DATE_POSTED = VALID_DATE_FILTER_VALUES

VALID_COUNTRIES = ["all", "", "IN", "US", "GB", "CA", "AU", "DE"]


class FilterSpec(BaseModel):
    """
    Immutable canonical FilterSpec model (Filter Integrity Lock).
    Computes SHA-256 integrity hash at creation and verifies lock prior to scraper dispatch.
    """
    job_title: str = "all"
    platform: str = "all"
    country: str = "all"
    remote_only: bool = False
    date_posted: str = "all"
    job_type: str = "all"
    q: Optional[str] = None
    integrity_hash: str = ""

    model_config = ConfigDict(frozen=True)

    @field_validator("job_type", mode="before")
    @classmethod
    def validate_job_type(cls, v):
        if not v or v.lower() not in [x.lower() for x in VALID_JOB_TYPES]:
            raise ValueError(f"Invalid job_type filter value: '{v}'")
        return v.lower().strip()

    @field_validator("date_posted", mode="before")
    @classmethod
    def validate_date_posted(cls, v):
        """
        AUDIT NOTE: this validator used to RAISE on any unrecognized value,
        which surfaced as a 400 to the user. Now that the supported date
        windows have been reduced to past_12h / past_24h / past_7d / past_30d,
        an old bookmark or cached frontend bundle still sending e.g.
        "past_10m" would hard-fail the whole search. Unknown values now fall
        back to the default window instead (resolve_cutoff_minutes() applies
        the same fallback), so a stale client degrades gracefully rather than
        erroring.
        """
        if not v or not isinstance(v, str):
            return "all"
        key = v.strip().lower()
        if key not in [x.lower() for x in VALID_DATE_POSTED]:
            import logging as _logging
            _logging.getLogger(__name__).info(
                f"[FilterSpec] Unsupported date_posted '{v}' -- falling back to default window."
            )
            return "past_7d"
        return key

    def get_serializable_dict(self) -> dict:
        return {
            "job_title": self.job_title,
            "platform": self.platform,
            "country": self.country,
            "remote_only": self.remote_only,
            "date_posted": self.date_posted,
            "job_type": self.job_type,
            "q": (self.q or "").strip(),
        }

    def compute_hash(self) -> str:
        serialized = json.dumps(self.get_serializable_dict(), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.compute_hash() == self.integrity_hash


def clean_param(val: Any, default: str = "all") -> str:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return default


def parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ["true", "1", "yes"]
    return bool(val)


def create_locked_filter_spec(
    job_title: Any = "all",
    platform: Any = "all",
    country: Any = "all",
    remote_only: Any = False,
    date_posted: Any = "all",
    job_type: Any = "all",
    q: Any = None,
) -> FilterSpec:
    """
    Creates and locks an immutable, hash-verified FilterSpec object.
    job_title is accepted as-is (any free text, comma-separated terms
    allowed) -- no restriction to a fixed title list.
    """
    t_clean = clean_param(job_title, "all")
    p_clean = clean_param(platform, "all").lower()
    c_clean = clean_param(country, "all").upper()
    d_clean = clean_param(date_posted, "all").lower()
    j_clean = clean_param(job_type, "all").lower()
    q_clean = clean_param(q, "").strip() if isinstance(q, str) and q.strip() else None
    r_bool = parse_bool(remote_only)

    raw_dict = {
        "job_title": t_clean,
        "platform": p_clean,
        "country": c_clean,
        "remote_only": r_bool,
        "date_posted": d_clean,
        "job_type": j_clean,
        "q": q_clean,
        "integrity_hash": "",
    }

    temp_spec = FilterSpec(**raw_dict)
    calc_hash = temp_spec.compute_hash()

    locked_spec = FilterSpec(
        job_title=temp_spec.job_title,
        platform=temp_spec.platform,
        country=temp_spec.country,
        remote_only=temp_spec.remote_only,
        date_posted=temp_spec.date_posted,
        job_type=temp_spec.job_type,
        q=temp_spec.q,
        integrity_hash=calc_hash,
    )

    if not locked_spec.verify_integrity():
        raise RuntimeError("FilterSpec integrity hash verification failed immediately after creation!")

    return locked_spec
