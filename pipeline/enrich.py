import re
import logging
from typing import Dict, Any, Optional, Tuple, List
from pipeline.normalize import NormalizedJob

logger = logging.getLogger(__name__)

GENERIC_EMAIL_PREFIXES = {
    "careers",
    "jobs",
    "job",
    "hiring",
    "hr",
    "recruitment",
    "recruiting",
    "privacy",
    "support",
    "info",
    "help",
    "contact",
    "admin",
    "sales",
    "inquiries",
}

# Known domain map for major tech companies
COMPANY_DOMAIN_MAP = {
    "stripe": "stripe.com",
    "datadog": "datadoghq.com",
    "okta": "okta.com",
    "cloudflare": "cloudflare.com",
    "airbnb": "airbnb.com",
    "figma": "figma.com",
    "gitlab": "gitlab.com",
    "spotify": "spotify.com",
    "palantir": "palantir.com",
    "netflix": "netflix.com",
    "zapier": "zapier.com",
    "chime": "chime.com",
    "robinhood": "robinhood.com",
}


def is_generic_email(email: str) -> bool:
    """Checks if an email address belongs to a generic company mailbox."""
    if not email or "@" not in email:
        return False
    local_part = email.split("@")[0].lower().strip()
    return local_part in GENERIC_EMAIL_PREFIXES


def extract_emails(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts recruiter email and company contact email from text.

    Returns:
        Tuple of (recruiter_email, company_contact_email)
    """
    if not text:
        return None, None

    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    matches = re.findall(email_pattern, text)

    recruiter_email = None
    company_contact_email = None

    for email in matches:
        email_clean = email.strip().lower()
        if is_generic_email(email_clean):
            if not company_contact_email:
                company_contact_email = email_clean
        else:
            if not recruiter_email:
                recruiter_email = email_clean

    return recruiter_email, company_contact_email


def extract_recruiter_name_near_email(text: str, recruiter_email: str, window_size: int = 150) -> Optional[str]:
    """
    Extracts recruiter name ONLY if a recruiter email was found and a name pattern exists
    within the same text block (~150 characters around the email).
    """
    if not text or not recruiter_email:
        return None

    email_idx = text.lower().find(recruiter_email.lower())
    if email_idx == -1:
        return None

    start_idx = max(0, email_idx - window_size)
    end_idx = min(len(text), email_idx + len(recruiter_email) + window_size)
    text_block = text[start_idx:end_idx]

    name_patterns = [
        r"(?:recruiter|hiring manager|contact|posted by|reach out to)\s*:\s*([A-Z][a-z]+\s+[A-Z][a-z]+)",
        r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[\(<]\s*" + re.escape(recruiter_email),
        r"(?:reach out to|contact)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b",
    ]

    for pattern in name_patterns:
        match = re.search(pattern, text_block, re.IGNORECASE)
        if match:
            candidate_name = match.group(1).strip()
            if len(candidate_name) <= 50 and not any(w in candidate_name.lower() for w in ["email", "apply", "job", "career"]):
                return candidate_name.title()

    local_part = recruiter_email.split("@")[0]
    if "." in local_part:
        parts = local_part.split(".")
        if len(parts) == 2 and all(p.isalpha() and len(p) >= 2 for p in parts):
            return f"{parts[0].capitalize()} {parts[1].capitalize()}"

    return None


def infer_company_contact_email(company_name: str) -> Optional[str]:
    """Infers canonical company contact email from company name."""
    if not company_name:
        return None
    comp_clean = re.sub(r"[^\w\s]", "", company_name.lower()).strip()
    domain = COMPANY_DOMAIN_MAP.get(comp_clean)
    if not domain and comp_clean and len(comp_clean) > 2 and " " not in comp_clean:
        domain = f"{comp_clean}.com"
    if domain:
        return f"careers@{domain}"
    return None


class RecruiterEnricher:
    """
    Enriches NormalizedJob instances with recruiter name, recruiter email,
    and generic company contact email.
    """

    def enrich_job(self, job: NormalizedJob) -> NormalizedJob:
        text = job.description_snippet or ""

        recruiter_email, company_contact_email = extract_emails(text)

        # Fallback to company domain contact email if explicit email not found in text
        if not company_contact_email:
            company_contact_email = infer_company_contact_email(job.company)

        recruiter_name = None
        if recruiter_email:
            recruiter_name = extract_recruiter_name_near_email(text, recruiter_email)

        job.recruiter_email = recruiter_email
        job.company_contact_email = company_contact_email
        job.recruiter_name = recruiter_name

        return job

    def enrich_batch(self, jobs: List[NormalizedJob]) -> List[NormalizedJob]:
        enriched_count = 0
        for job in jobs:
            self.enrich_job(job)
            if job.recruiter_email or job.company_contact_email or job.recruiter_name:
                enriched_count += 1

        logger.info(f"Enrichment completed. Enriched contacts for {enriched_count} / {len(jobs)} jobs.")
        return jobs
