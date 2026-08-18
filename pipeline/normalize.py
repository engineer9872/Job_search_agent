import html
import re
import logging
import email.utils
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Tuple, Dict, List
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# Currency mapping by country code
COUNTRY_CURRENCY_MAP = {
    "us": "USD",
    "gb": "GBP",
    "uk": "GBP",
    "in": "INR",
    "ca": "CAD",
    "au": "AUD",
    "de": "EUR",
    "fr": "EUR",
    "nl": "EUR",
    "sg": "SGD",
    "nz": "NZD",
}


def strip_html(text: Optional[str]) -> str:
    """Removes HTML tags, unescapes HTML entities (&quot;, &amp;, &nbsp;, &#39;), and normalizes whitespace."""
    if not text:
        return ""
    # Unescape HTML entities
    unescaped = html.unescape(text)
    # Remove HTML tags
    clean = re.sub(r"<[^>]*>", "", unescaped)
    return " ".join(clean.split())


def extract_work_authorization_note(description: str) -> Optional[str]:
    """Scans job description to extract US or other work authorization requirements."""
    if not description:
        return None
    text = description.lower()
    phrases = [
        "must be authorized to work",
        "authorized to work in the",
        "eligible to work in the",
        "no sponsorship",
        "visa sponsorship",
        "sponsorship is not available",
        "sponsorship not available",
        "unable to sponsor",
        "cannot sponsor",
        "not offer sponsorship",
        "us citizen",
        "citizenship required",
        "citizen only",
        "green card",
        "work authorization required",
        "work authorisation required"
    ]
    # Split by common sentence/line punctuation
    sentences = re.split(r'[.!?\n]', description)
    for sentence in sentences:
        s_clean = sentence.strip()
        s_lower = s_clean.lower()
        if any(phrase in s_lower for phrase in phrases):
            return s_clean[:200]
    return None


class NormalizedJob(BaseModel):
    """
    Standardized Pydantic data model matching the jobs table schema.
    """
    title: str = Field(..., max_length=255)
    canonical_title: Optional[str] = Field(default=None, max_length=100)
    skills: Optional[str] = None
    company: str = Field(..., max_length=255)
    city: Optional[str] = Field(default=None, max_length=100)
    country: Optional[str] = Field(default=None, max_length=100)
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = Field(default=None, max_length=10)
    remote_flag: bool = False
    job_type: Optional[str] = Field(default="unknown", max_length=50)
    source_platform: str = Field(..., max_length=50)
    apply_url: str
    description_snippet: Optional[str] = None
    posted_date: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recruiter_name: Optional[str] = Field(default=None, max_length=255)
    recruiter_email: Optional[str] = Field(default=None, max_length=255)
    company_contact_email: Optional[str] = Field(default=None, max_length=255)
    work_authorization_note: Optional[str] = None


    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="after")
    def populate_work_auth_note(self) -> 'NormalizedJob':
        if not self.work_authorization_note and self.description_snippet:
            self.work_authorization_note = extract_work_authorization_note(self.description_snippet)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Converts model to dictionary suitable for SQLAlchemy insert."""
        return self.model_dump()


def detect_remote(title: str, description: str, location_str: str) -> bool:
    """Detects if a job is remote based on text patterns."""
    text = f"{title} {description} {location_str}".lower()
    patterns = [r"\bremote\b", r"\bwork from home\b", r"\bwfh\b", r"\btelecommute\b", r"\bhybrid\b"]
    return any(re.search(pat, text) for pat in patterns)


import email.utils

def parse_date(date_val: Any) -> Optional[datetime]:
    """Parses date string, relative date text ('2 days ago'), RFC 822 date, or Unix timestamp into datetime object."""
    if not date_val:
        return None
    try:
        if isinstance(date_val, (int, float)):
            # Epoch milliseconds or seconds
            if date_val > 1e11:
                date_val = date_val / 1000.0
            return datetime.fromtimestamp(date_val, tz=timezone.utc)
        if isinstance(date_val, str):
            # Handle numeric strings (epoch ms or seconds, e.g. from Naukri epicscrapers)
            stripped = date_val.strip()
            if stripped.isdigit():
                ts = float(stripped)
                if ts > 1e11:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            val_lower = stripped.lower()
            now = datetime.now(timezone.utc)

            # -----------------------------------------------------------
            # RELATIVE-DATE PARSING. Order matters, and it was wrong.
            #
            # Three bugs fixed here, all of which silently FABRICATED dates:
            #
            #  1. "Today" was caught by the `"day" in val_lower` branch before
            #     the explicit today check ever ran, and with no digit present
            #     it defaulted to 1 -> "Today" became YESTERDAY.
            #  2. Any weekday name contains "day". "Monday, 18 July 2026" hit
            #     the same branch, re.search grabbed the first number it saw
            #     (18) and returned "18 days ago" -> 30 July. A real, exact
            #     date was turned into a wrong one.
            #  3. A missing digit silently became 1 ("a few hours ago" -> 1h),
            #     which invents precision the source never gave.
            #
            # Now: exact phrases are matched FIRST, weekday names are excluded,
            # and a relative phrase with no number returns None (unknown)
            # rather than a made-up value. None is honest; a wrong timestamp
            # is worse than no timestamp.
            # -----------------------------------------------------------
            WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday")

            if val_lower in ("today", "just now", "just posted", "posted today"):
                return now
            if val_lower in ("yesterday", "posted yesterday"):
                return now - timedelta(days=1)

            # A weekday name is a CALENDAR reference, not a relative offset.
            # Let the ISO / RFC-822 parsers below handle it.
            if not any(w in val_lower for w in WEEKDAYS):
                rel = re.search(
                    r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago",
                    val_lower,
                )
                if rel:
                    amount, unit = int(rel.group(1)), rel.group(2)
                    if unit in ("minute", "min"):
                        return now - timedelta(minutes=amount)
                    if unit in ("hour", "hr"):
                        return now - timedelta(hours=amount)
                    if unit == "day":
                        return now - timedelta(days=amount)
                    if unit == "week":
                        return now - timedelta(weeks=amount)
                    if unit == "month":
                        return now - timedelta(days=amount * 30)
                    if unit == "year":
                        return now - timedelta(days=amount * 365)

                # A relative phrase we recognise but cannot quantify
                # ("a few days ago", "several weeks ago"). Returning a guess
                # here is what let vague text masquerade as a precise date.
                if "ago" in val_lower:
                    return None

            # Attempt RFC 822 / 2822 RSS date parsing
            try:
                dt_parsed = email.utils.parsedate_to_datetime(date_val)
                if dt_parsed:
                    return dt_parsed
            except Exception:
                pass

            # Attempt standard ISO format parsing
            try:
                cleaned = date_val.replace("Z", "+00:00")
                return datetime.fromisoformat(cleaned)
            except Exception:
                pass

            # Fallback to dateutil parser if available
            try:
                from dateutil import parser as dt_parser
                return dt_parser.parse(date_val)
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"Could not parse date '{date_val}': {e}")
    return None



INDIAN_CITIES = {
    "bengaluru", "bangalore", "hyderabad", "mumbai", "delhi", "new delhi",
    "gurgaon", "gurugram", "noida", "pune", "chennai", "kolkata",
    "ahmedabad", "kochi", "trivandrum", "thiruvananthapuram", "indore", "jaipur"
}


US_CITIES = [
    "washington", "mclean", "reston", "silver spring", "adelphi", "san francisco", "sf",
    "new york", "nyc", "austin", "seattle", "chicago", "boston", "denver", "los angeles",
    "la", "san jose", "atlanta", "dallas", "houston", "miami", "phoenix", "portland",
    "san diego", "baltimore", "pittsburgh", "philadelphia", "charlotte", "raleigh"
]

UK_CITIES = [
    "london", "manchester", "haddenham", "birmingham", "leeds", "edinburgh", "bristol",
    "glasgow", "cambridge", "oxford", "liverpool"
]

CA_CITIES = [
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "edmonton", "quebec"
]

AU_CITIES = [
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra"
]

DE_CITIES = [
    "berlin", "munich", "frankfurt", "hamburg", "cologne", "stuttgart", "dusseldorf"
]

# US state abbreviations — used to reliably detect "City, ST" as a US location
# BEFORE falling back to the search's default_country. This is what fixes the
# India/Indiana collision: "IN" is both India's ISO code and Indiana's state
# code, so a raw location like "Cincinnati, OH" or "Westerville, IN" must be
# resolved from the actual state token, never guessed from the search filter.
US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def extract_city_and_country(location_data: Any, default_country: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts city and country code cleanly from location string or dict.
    Prevents awkward outputs like 'Worldwide, US' or 'Washington, IN'.
    """
    def_c = default_country.strip().upper() if default_country else None

    if not location_data:
        return None, def_c

    loc_str = ""
    if isinstance(location_data, str):
        loc_str = location_data.strip()
    elif isinstance(location_data, dict):
        loc_str = location_data.get("name") or location_data.get("display_name") or ""
        if not loc_str and location_data.get("area"):
            loc_str = ", ".join(location_data.get("area"))

    if not loc_str:
        return None, def_c

    loc_lower = loc_str.lower().strip()

    # 1. Global / Remote descriptors
    if loc_lower in ["worldwide", "global", "anywhere", "remote", "wfh", "work from home"] or any(loc_lower.startswith(w) for w in ["worldwide,", "global,", "anywhere,", "remote,"]):
        return None, "Remote / Global"

    # 2. Regional descriptors
    if any(r in loc_lower for r in ["americas", "latin america", "latam"]):
        return None, "Americas"
    if any(r in loc_lower for r in ["europe", "emea"]):
        return None, "Europe"
    if any(r in loc_lower for r in ["apac", "asia pacific"]):
        return None, "Asia Pacific"

    # 3. Country-level locations (not cities)
    if any(loc_lower.startswith(c) for c in ["usa,", "united states,", "us,"]) or loc_lower in ["usa", "united states", "us"]:
        return None, "US"
    if any(loc_lower.startswith(c) for c in ["india,", "in,"]) or loc_lower in ["india", "in"]:
        return None, "IN"
    if any(loc_lower.startswith(c) for c in ["uk,", "united kingdom,", "gb,"]) or loc_lower in ["uk", "united kingdom", "gb", "great britain"]:
        return None, "GB"
    if any(loc_lower.startswith(c) for c in ["canada,", "ca,"]) or loc_lower in ["canada", "ca"]:
        return None, "CA"
    if any(loc_lower.startswith(c) for c in ["australia,", "au,"]) or loc_lower in ["australia", "au"]:
        return None, "AU"
    if any(loc_lower.startswith(c) for c in ["germany,", "de,"]) or loc_lower in ["germany", "de", "deutschland"]:
        return None, "DE"


    # 4. Handle prefixed codes like "IN-Bengaluru" or "US-San Francisco"
    city = None
    country = None

    if "-" in loc_str:
        parts = loc_str.split("-", 1)
        if len(parts[0].strip()) == 2 and parts[0].strip().isalpha():
            country = parts[0].strip().upper()
            city = parts[1].strip()

    if not city:
        parts = [p.strip() for p in loc_str.split(",") if p.strip()]
        city = parts[0] if parts else loc_str

        # Explicit US state-code detection: "City, ST" (e.g. "Cincinnati, OH",
        # "Westerville, IN") is an unambiguous US signal and must be checked
        # BEFORE any city-name list or default_country fallback — otherwise
        # unrecognized US cities silently inherit whatever country the user
        # searched for, which is the bug that showed Indiana jobs under India.
        if len(parts) >= 2:
            state_token = parts[1].strip().upper()
            state_token_clean = re.sub(r"[^A-Z]", "", state_token)[:2]
            if state_token_clean in US_STATE_ABBR:
                country = "US"

    # City-level country detection with explicit override
    if country:
        pass
    elif any(c in loc_lower for c in INDIAN_CITIES) or "india" in loc_lower:
        country = "IN"
    elif any(c in loc_lower for c in US_CITIES) or "united states" in loc_lower or "usa" in loc_lower:
        country = "US"
    elif any(c in loc_lower for c in UK_CITIES) or "united kingdom" in loc_lower:
        country = "GB"
    elif any(c in loc_lower for c in CA_CITIES) or "canada" in loc_lower:
        country = "CA"
    elif any(c in loc_lower for c in AU_CITIES) or "australia" in loc_lower:
        country = "AU"
    elif any(c in loc_lower for c in DE_CITIES) or "germany" in loc_lower:
        country = "DE"
    else:
        country = country or def_c

    # Prevent city from repeating country name
    if city and city.upper() == country:
        city = None

    return (city[:100] if city else None, country[:100] if country else def_c)




def extract_city(location_data: Any) -> Optional[str]:
    city, _ = extract_city_and_country(location_data)
    return city


CONTRACT_ONLY_PORTALS = {
    "upwork", "fiverr", "toptal", "freelancer", "guru", "peopleperhour", "truelancer", "contra"
}


def normalize_job_type(portal_id: str, raw_job_type: Optional[str] = None, title: str = "", description: str = "") -> str:
    """
    Normalizes job type into enum: full_time, contract, part_time, onsite_only, unknown.
    Hardcodes 'contract' for inherent contract portals per specification.
    """
    p_lower = portal_id.lower().strip()
    if p_lower in CONTRACT_ONLY_PORTALS:
        return "contract"

    text = f"{raw_job_type or ''} {title} {description}".lower()
    if any(k in text for k in ["contract", "contractor", "freelance", "temporary", "consultant"]):
        return "contract"
    if any(k in text for k in ["full time", "full-time", "full_time", "permanent"]):
        return "full_time"
    if any(k in text for k in ["part time", "part-time", "part_time"]):
        return "part_time"
    if "onsite" in text or "on-site" in text:
        return "onsite_only"

    return "unknown"


def match_canonical_title(raw_title: str) -> Optional[str]:
    """Matches raw job title against the 12 canonical job titles. Returns canonical title or None."""
    if not raw_title:
        return None
    t_clean = raw_title.strip().lower()

    if "servicenow" in t_clean:
        return "ServiceNow Engineer"
    if "data scientist" in t_clean or "data science" in t_clean:
        return "Data Scientist"
    if "machine learning" in t_clean or re.search(r"\bml\b", t_clean) or "deep learning" in t_clean:
        return "Machine Learning Engineer"
    if "ai engineer" in t_clean or "artificial intelligence" in t_clean or "llm engineer" in t_clean or "genai" in t_clean:
        return "AI Engineer"
    if "data engineer" in t_clean:
        return "Data Engineer"
    if "devops" in t_clean or "infrastructure engineer" in t_clean or "platform engineer" in t_clean:
        return "DevOps Engineer"
    if "cloud" in t_clean or "aws" in t_clean or "azure" in t_clean or "gcp" in t_clean:
        return "Cloud Engineer / Architect"
    if "product manager" in t_clean or "technical product manager" in t_clean or re.search(r"\btpm\b", t_clean):
        return "Product Manager"
    if "qa" in t_clean or "test engineer" in t_clean or "sdet" in t_clean or "quality assurance" in t_clean:
        return "QA / Test Automation Engineer"
    if "sre" in t_clean or "site reliability" in t_clean:
        return "Site Reliability Engineer (SRE)"
    if "cyber" in t_clean or "security engineer" in t_clean or "information security" in t_clean:
        return "Cybersecurity Engineer"
    if any(w in t_clean for w in ["software", "engineer", "developer", "swe", "full stack", "fullstack", "backend", "frontend"]):
        return "Software Engineer"

    return None



# ---------------------------------------------------------------------------
# ADZUNA NORMALIZER
# ---------------------------------------------------------------------------
def normalize_adzuna_job(raw_job: Dict[str, Any], country: str = "us") -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("title", ""))
        if not title:
            return None

        company_obj = raw_job.get("company", {})
        company = company_obj.get("display_name", "").strip() if isinstance(company_obj, dict) else str(company_obj)
        if not company:
            company = "Unknown Company"

        apply_url = raw_job.get("redirect_url", "").strip()
        if not apply_url:
            return None

        location_obj = raw_job.get("location", {})
        city, job_country = extract_city_and_country(location_obj, default_country=country.upper())

        description = strip_html(raw_job.get("description", ""))
        remote_flag = detect_remote(title, description, str(location_obj))

        salary_min = float(raw_job["salary_min"]) if raw_job.get("salary_min") is not None else None
        salary_max = float(raw_job["salary_max"]) if raw_job.get("salary_max") is not None else None
        currency = COUNTRY_CURRENCY_MAP.get(country.lower(), "USD")

        contract_time = raw_job.get("contract_time", "")
        contract_type = raw_job.get("contract_type", "")
        job_type_parts = [p for p in [contract_time, contract_type] if p]
        job_type = "-".join(job_type_parts) if job_type_parts else "full_time"

        posted_date = parse_date(raw_job.get("created"))

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=job_country[:100] if job_country else country.upper(),
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            remote_flag=remote_flag,
            job_type=job_type[:50],
            source_platform="adzuna",
            apply_url=apply_url,
            description_snippet=description,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing Adzuna job: {e}")
        return None


# ---------------------------------------------------------------------------
# JSEARCH NORMALIZER
# ---------------------------------------------------------------------------
def normalize_jsearch_job(raw_job: Dict[str, Any], country: str = "us") -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("job_title", ""))
        if not title:
            return None

        company = raw_job.get("employer_name") or raw_job.get("company_name", "Unknown Company")
        company = company.strip()

        apply_url = raw_job.get("job_apply_link") or raw_job.get("job_google_link") or ""
        if not apply_url and isinstance(raw_job.get("apply_options"), list) and len(raw_job["apply_options"]) > 0:
            first_opt = raw_job["apply_options"][0]
            if isinstance(first_opt, dict):
                apply_url = first_opt.get("apply_link") or ""

        apply_url = str(apply_url).strip()
        if not apply_url:
            return None


        city_raw = raw_job.get("job_city")
        country_req = country.strip().upper() if country else "US"
        city, job_country = extract_city_and_country(city_raw, default_country=country_req)
        job_country = job_country or country_req


        description = strip_html(raw_job.get("job_description", ""))
        is_remote_val = raw_job.get("job_is_remote", False)
        remote_flag = bool(is_remote_val) or detect_remote(title, description, str(city))

        salary_min = float(raw_job["job_min_salary"]) if raw_job.get("job_min_salary") is not None else None
        salary_max = float(raw_job["job_max_salary"]) if raw_job.get("job_max_salary") is not None else None
        currency = raw_job.get("job_salary_currency") or COUNTRY_CURRENCY_MAP.get(country.lower(), "USD")

        job_type = raw_job.get("job_employment_type", "FULLTIME").lower()
        posted_date = parse_date(raw_job.get("job_posted_at_datetime_utc"))

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=job_country[:100] if job_country else country.upper(),
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            remote_flag=remote_flag,
            job_type=job_type[:50],
            source_platform="jsearch",
            apply_url=apply_url,
            description_snippet=description,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing JSearch job: {e}")
        return None


# ---------------------------------------------------------------------------
# GREENHOUSE NORMALIZER
# ---------------------------------------------------------------------------
def normalize_greenhouse_job(raw_job: Dict[str, Any], company_name: Optional[str] = None) -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("title", ""))
        if not title:
            return None

        company = company_name or raw_job.get("_company_token", "Greenhouse Board").title()

        apply_url = raw_job.get("absolute_url", "").strip()
        if not apply_url:
            return None

        loc_obj = raw_job.get("location", {})
        loc_str = loc_obj.get("name", "") if isinstance(loc_obj, dict) else str(loc_obj)
        city, country = extract_city_and_country(loc_str)

        description = strip_html(raw_job.get("content", ""))
        remote_flag = detect_remote(title, description, loc_str)

        # `updated_at` is NOT a posting date -- Greenhouse bumps it whenever
        # anything on the listing changes, so a 30-day-old role edited this
        # morning would look four hours old. Prefer the real publication
        # fields; fall back to None (honest unknown) rather than to a field
        # that means something else.
        posted_date = parse_date(
            raw_job.get("first_published")
            or raw_job.get("created_at")
            or raw_job.get("published_at")
        )

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=country[:100] if country else None,
            salary_min=None,
            salary_max=None,
            currency=None,
            remote_flag=remote_flag,
            job_type="full_time",
            source_platform="greenhouse",
            apply_url=apply_url,
            description_snippet=description[:2000] if description else None,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing Greenhouse job: {e}")
        return None


# ---------------------------------------------------------------------------
# LEVER NORMALIZER
# ---------------------------------------------------------------------------
def normalize_lever_job(raw_job: Dict[str, Any], company_name: Optional[str] = None) -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("text", ""))
        if not title:
            return None

        company = company_name or raw_job.get("_company_slug", "Lever Board").title()

        apply_url = raw_job.get("hostedUrl", "").strip()
        if not apply_url:
            return None

        categories = raw_job.get("categories", {})
        loc_str = categories.get("location", "") if isinstance(categories, dict) else ""
        city, country = extract_city_and_country(loc_str)
        commitment = categories.get("commitment", "full_time") if isinstance(categories, dict) else "full_time"

        description = raw_job.get("descriptionPlain") or strip_html(raw_job.get("description", ""))
        remote_flag = detect_remote(title, description, loc_str)

        posted_date = parse_date(raw_job.get("createdAt"))

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=country[:100] if country else None,
            salary_min=None,
            salary_max=None,
            currency=None,
            remote_flag=remote_flag,
            job_type=str(commitment)[:50],
            source_platform="lever",
            apply_url=apply_url,
            description_snippet=description[:2000] if description else None,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing Lever job: {e}")
        return None


# ---------------------------------------------------------------------------
# REMOTIVE NORMALIZER
# ---------------------------------------------------------------------------
def normalize_remotive_job(raw_job: Dict[str, Any], country: str = "us") -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("title", ""))
        if not title:
            return None

        company = raw_job.get("company_name", "Unknown Company").strip()

        apply_url = raw_job.get("url", "").strip()
        if not apply_url:
            return None

        loc_str = raw_job.get("candidate_required_location", "")
        city, job_country = extract_city_and_country(loc_str, default_country=country.upper())

        description = strip_html(raw_job.get("description", ""))
        posted_date = parse_date(raw_job.get("publication_date"))

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=job_country[:100] if job_country else country.upper(),
            salary_min=None,
            salary_max=None,
            currency=None,
            remote_flag=True,
            job_type=str(raw_job.get("job_type", "full_time"))[:50],
            source_platform="remotive",
            apply_url=apply_url,
            description_snippet=description[:2000] if description else None,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing Remotive job: {e}")
        return None


# ---------------------------------------------------------------------------
# GOOGLE JOBS NORMALIZER (SERPAPI)
# ---------------------------------------------------------------------------
def normalize_google_job(raw_job: Dict[str, Any], country: str = "us") -> Optional[NormalizedJob]:
    try:
        title = strip_html(raw_job.get("title", ""))
        if not title:
            return None

        company = raw_job.get("company_name", "Unknown Company").strip()

        # ---------------------------------------------------------------
        # APPLY-URL EXTRACTION (MAX-JOBS FIX)
        #
        # This block used to fall back to `share_link`, which for SerpApi
        # Google Jobs results is a google.com/search?ibp=htl;jobs... link --
        # a Google SERP, not a job posting. 801 of 2,943 stored rows had one
        # of these as their apply_url. Every single one was then discarded by
        # ThreeTierFilterGuard at READ time as `invalid_or_indirect_url`, so
        # they were pure dead weight: they cost a scrape, occupied a row,
        # inflated "duplicates" counts, and could never be shown to a user.
        #
        # Now: walk EVERY apply option (not just the first), take the first
        # genuinely usable link, and if none exists REJECT THE ROW HERE rather
        # than persisting something the read path will always throw away.
        # ---------------------------------------------------------------
        _usable = is_usable_apply_url

        apply_url = ""
        apply_options = raw_job.get("apply_options", [])
        if isinstance(apply_options, list):
            for opt in apply_options:
                if not isinstance(opt, dict):
                    continue
                cand = (opt.get("link") or opt.get("apply_link") or "").strip()
                if _usable(cand):
                    apply_url = cand
                    break

        if not apply_url:
            for key in ("job_apply_link", "apply_link", "url", "link", "share_link"):
                cand = str(raw_job.get(key) or "").strip()
                if _usable(cand):
                    apply_url = cand
                    break

        apply_url = str(apply_url).strip()
        if not apply_url:
            logger.debug(
                "[Normalize] Dropping job with no usable apply URL "
                f"(title={str(raw_job.get('title'))[:60]!r}) -- refusing to persist a "
                "row the read path would always reject."
            )
            return None

        loc_str = raw_job.get("location", "")
        city, job_country = extract_city_and_country(loc_str, default_country=country.upper())

        description = strip_html(raw_job.get("description", ""))
        detected_ext = raw_job.get("detected_extensions", {})
        is_remote_val = detected_ext.get("work_from_home", False) if isinstance(detected_ext, dict) else False
        remote_flag = bool(is_remote_val) or detect_remote(title, description, loc_str)

        posted_str = detected_ext.get("posted_at") if isinstance(detected_ext, dict) else None
        posted_date = parse_date(posted_str)

        job_type = detected_ext.get("schedule_type", "full_time") if isinstance(detected_ext, dict) else "full_time"

        return NormalizedJob(
            title=title[:255],
            company=company[:255],
            city=city[:100] if city else None,
            country=job_country[:100] if job_country else country.upper(),
            salary_min=None,
            salary_max=None,
            currency=None,
            remote_flag=remote_flag,
            job_type=str(job_type)[:50],
            source_platform="google_jobs",
            apply_url=apply_url,
            description_snippet=description[:2000] if description else None,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing Google job: {e}")
        return None


# ---------------------------------------------------------------------------
# GENERIC CONNECTOR NORMALIZER (Naukri / Indeed / LinkedIn / Glassdoor)
# ---------------------------------------------------------------------------
def is_usable_apply_url(u: Any) -> bool:
    """
    THE single test for "can a user actually apply through this link?".

    Search-engine result pages are NOT job postings. SerpApi Google Jobs items
    frequently carry a `share_link` / first `apply_options[0]` of the form
    google.com/search?ibp=htl;jobs..., and those were being persisted as
    apply_url. 801 of 2,943 stored rows were exactly this -- every one of them
    discarded by ThreeTierFilterGuard at READ time as `invalid_or_indirect_url`.
    They cost a scrape, occupied a row, inflated duplicate ratios, and could
    never be shown to anyone.

    Used by every normalizer so a row like that is rejected at WRITE time.
    """
    u = str(u or "").strip().lower()
    if not u.startswith("http"):
        return False
    bad = [
        "google.com/search", "google.com/url?", "bing.com/search",
        "duckduckgo.com/?q=", "ibp=htl;jobs", "search_query=",
        "/jobs/search/?keywords=", "/jobs/search?keywords=",
    ]
    return not any(b in u for b in bad)


def normalize_connector_job(
    raw_job: Dict[str, Any],
    source_platform: str = "linkedin",
    country: str = "IN",
) -> Optional["NormalizedJob"]:
    """
    Normalizes the standardized dict format returned by our Apify-based connectors
    (naukri, indeed, linkedin, glassdoor). Their raw dicts already contain:
      title, company, url, location, remote, contract_type, posted_date, description
    """
    try:
        title = str(raw_job.get("title") or "").strip()
        if not title:
            return None

        # MAX-JOBS FIX: walk every candidate key rather than giving up after
        # `url`/`link`, then reject anything a user could not actually apply
        # through. Recovering a real link here is worth more than any read-time
        # filter tweak -- a row with a usable URL is a job the user can SEE.
        apply_url = ""
        for _key in ("url", "link", "apply_url", "applyUrl", "jobUrl",
                     "job_url", "apply_link", "detail_url"):
            _cand = str(raw_job.get(_key) or "").strip()
            if is_usable_apply_url(_cand):
                apply_url = _cand
                break
        if not apply_url:
            return None

        company = str(raw_job.get("company") or raw_job.get("companyName") or f"{source_platform.capitalize()} Employer").strip()
        if isinstance(raw_job.get("company"), dict):
            company = raw_job["company"].get("name") or company

        loc_raw = raw_job.get("location") or country
        city, job_country = extract_city_and_country(loc_raw, default_country=country.upper())

        description = strip_html(str(raw_job.get("description") or ""))
        remote_flag = bool(raw_job.get("remote")) or detect_remote(title, description, str(loc_raw))

        posted_date = parse_date(raw_job.get("posted_date") or raw_job.get("postedAt") or raw_job.get("postedDate"))

        contract_type_raw = raw_job.get("job_type") or raw_job.get("contract_type") or raw_job.get("employmentType") or "unknown"
        job_type = normalize_job_type(source_platform, contract_type_raw, title, description)

        canonical = match_canonical_title(title)

        return NormalizedJob(
            title=title[:255],
            canonical_title=canonical,
            company=company[:255],
            city=city[:100] if city else None,
            country=job_country[:100] if job_country else country.upper(),
            salary_min=None,
            salary_max=None,
            currency=COUNTRY_CURRENCY_MAP.get(country.lower()),
            remote_flag=remote_flag,
            job_type=job_type,
            source_platform=source_platform[:50],
            apply_url=apply_url,
            description_snippet=description[:2000] if description else None,
            posted_date=posted_date,
            fetched_at=datetime.now(timezone.utc),
            recruiter_email=None,
        )
    except Exception as e:
        logger.error(f"Error normalizing {source_platform} job: {e}")
        return None


# ---------------------------------------------------------------------------
# UNIFIED NORMALIZATION DISPATCHER
# ---------------------------------------------------------------------------
def normalize_job_batch(
    raw_jobs: List[Dict[str, Any]],
    source_platform: str = "adzuna",
    country: str = "us",
    company_name: Optional[str] = None,
) -> List[NormalizedJob]:
    """
    Normalizes a list of raw job records based on source platform.
    """
    normalized_list = []
    platform = source_platform.lower()

    for raw in raw_jobs:
        job = None
        if platform == "adzuna":
            job = normalize_adzuna_job(raw, country=country)
        elif platform == "jsearch":
            job = normalize_jsearch_job(raw, country=country)
        elif platform == "remotive":
            job = normalize_remotive_job(raw, country=country)
        elif platform in ["google_jobs", "google"]:
            job = normalize_google_job(raw, country=country)
        elif platform == "greenhouse":
            job = normalize_greenhouse_job(raw, company_name=company_name)
        elif platform == "lever":
            job = normalize_lever_job(raw, company_name=company_name)
        elif platform in [
            "linkedin", "indeed", "glassdoor",
            "dice", "ziprecruiter", "usajobs",
            "careerbuilder", "simplyhired", "weworkremotely", "hired",
        ]:
            job = normalize_connector_job(raw, source_platform=platform, country=country)
        else:
            job = normalize_connector_job(raw, source_platform=platform, country=country)

        if job:
            normalized_list.append(job)

    return normalized_list


