"""
HYBRID SEARCH — query expansion.

THE PROBLEM
-----------
Searching "cloud engineer" used to return only titles literally containing
"cloud" and "engineer". A "Platform Engineer (AWS, Kubernetes)" or a "Site
Reliability Engineer" is the SAME JOB to a candidate, and both were invisible.
Job titles are not standardised: one company's "Cloud Engineer" is another's
"Infrastructure Engineer" and another's "DevOps Engineer".

Exact-only matching therefore hides most of the real market. But naive
expansion is worse -- widen too far and a "cloud engineer" search fills up with
"Sales Engineer" and "Network Technician", which is exactly the noise this
project exists to remove.

THE APPROACH
------------
Expansion is TIERED, and the tier travels with the result so ranking can keep
genuine matches on top:

  CORE     the words the user actually typed          -> relevance 0.85 - 1.00
  STRONG   a different name for the same job          -> relevance ~0.70
           ("cloud engineer" -> "platform engineer", "SRE")
  RELATED  adjacent, plausibly interesting            -> relevance ~0.50
           ("cloud engineer" -> "backend engineer")

Nothing below RELATED is ever added. The taxonomy is hand-curated rather than
generated, because a sloppy synonym list is precisely how "engineer" starts
matching "sales engineer".

Every non-title filter (date, platform, country, remote, job type) stays
EXACT. Expansion widens only what "matching the search text" means.
"""

import re
from typing import Dict, List, Set, Tuple

TIER_CORE = "core"
TIER_STRONG = "strong"
TIER_RELATED = "related"

TIER_WEIGHT = {TIER_CORE: 1.0, TIER_STRONG: 0.70, TIER_RELATED: 0.50}


# ---------------------------------------------------------------------------
# ROLE FAMILIES
#
# `strong`  : different names for substantially the same job
# `related` : adjacent roles a candidate for this would plausibly also want
# `skills`  : technologies that identify the role even when the title is vague
#             ("Engineer II" at a company whose stack is Terraform + K8s)
# ---------------------------------------------------------------------------
ROLE_FAMILIES: List[Dict] = [
    {
        "name": "cloud_infra",
        "match": ["cloud engineer", "cloud", "devops", "sre", "site reliability",
                  "platform engineer", "infrastructure engineer", "cloud architect"],
        "strong": ["devops engineer", "site reliability engineer", "platform engineer",
                   "infrastructure engineer", "cloud engineer", "cloud architect",
                   "systems engineer", "sre"],
        "related": ["backend engineer", "build engineer", "release engineer",
                    "network engineer", "solutions architect"],
        "skills": ["aws", "azure", "gcp", "kubernetes", "terraform", "docker",
                   "ansible", "cloudformation", "openshift"],
    },
    {
        "name": "backend",
        "match": ["backend", "back end", "back-end", "server side", "api engineer"],
        "strong": ["backend engineer", "backend developer", "software engineer",
                   "api engineer", "server side engineer"],
        "related": ["full stack engineer", "platform engineer", "microservices engineer"],
        "skills": ["java", "python", "golang", "node.js", "spring boot", ".net",
                   "django", "fastapi", "microservices"],
    },
    {
        "name": "frontend",
        "match": ["frontend", "front end", "front-end", "ui engineer", "ui developer"],
        "strong": ["frontend engineer", "frontend developer", "ui engineer",
                   "web developer", "javascript developer"],
        "related": ["full stack engineer", "mobile developer", "ux engineer"],
        "skills": ["react", "angular", "vue", "typescript", "next.js", "tailwind"],
    },
    {
        "name": "fullstack",
        "match": ["full stack", "fullstack", "full-stack"],
        "strong": ["full stack engineer", "full stack developer", "software engineer"],
        "related": ["backend engineer", "frontend engineer", "web developer"],
        "skills": ["react", "node.js", "python", "typescript", "mern"],
    },
    {
        "name": "data_eng",
        "match": ["data engineer", "etl", "data pipeline", "analytics engineer"],
        "strong": ["data engineer", "etl developer", "analytics engineer",
                   "big data engineer", "data platform engineer"],
        "related": ["data scientist", "business intelligence engineer",
                    "backend engineer", "database engineer"],
        "skills": ["spark", "airflow", "snowflake", "dbt", "kafka", "databricks",
                   "redshift", "bigquery"],
    },
    {
        "name": "data_sci",
        "match": ["data scientist", "data science"],
        "strong": ["data scientist", "applied scientist", "research scientist",
                   "quantitative analyst"],
        "related": ["machine learning engineer", "data analyst", "data engineer",
                    "statistician"],
        "skills": ["pandas", "scikit-learn", "r", "statistics", "sql", "jupyter"],
    },
    {
        "name": "ml_ai",
        "match": ["machine learning", "ml engineer", "ai engineer",
                  "artificial intelligence", "deep learning", "nlp"],
        "strong": ["machine learning engineer", "ai engineer", "ml engineer",
                   "deep learning engineer", "nlp engineer", "mlops engineer"],
        "related": ["data scientist", "research engineer", "computer vision engineer",
                    "data engineer"],
        "skills": ["pytorch", "tensorflow", "llm", "hugging face", "mlops",
                   "computer vision", "transformers"],
    },
    {
        "name": "qa",
        "match": ["qa", "quality assurance", "test automation", "sdet", "tester"],
        "strong": ["qa engineer", "sdet", "test automation engineer",
                   "quality assurance engineer", "automation engineer"],
        "related": ["software engineer", "performance engineer", "release engineer"],
        "skills": ["selenium", "cypress", "playwright", "junit", "pytest", "appium"],
    },
    {
        "name": "security",
        "match": ["security", "cybersecurity", "infosec", "appsec", "soc analyst"],
        "strong": ["security engineer", "cybersecurity engineer", "application security engineer",
                   "information security analyst", "soc analyst"],
        "related": ["devsecops engineer", "cloud engineer", "network engineer",
                    "penetration tester"],
        "skills": ["siem", "penetration testing", "owasp", "iam", "splunk", "soc2"],
    },
    {
        "name": "mobile",
        "match": ["mobile", "android", "ios", "flutter", "react native"],
        "strong": ["mobile developer", "android developer", "ios developer",
                   "flutter developer", "react native developer"],
        "related": ["frontend engineer", "full stack engineer"],
        "skills": ["swift", "kotlin", "jetpack compose", "swiftui", "dart"],
    },
    {
        "name": "product",
        "match": ["product manager", "product owner", "pm", "product management"],
        "strong": ["product manager", "senior product manager", "product owner",
                   "technical product manager"],
        "related": ["program manager", "business analyst", "project manager"],
        "skills": ["roadmap", "stakeholder", "agile", "scrum", "backlog"],
    },
    {
        "name": "servicenow",
        "match": ["servicenow", "service now"],
        "strong": ["servicenow developer", "servicenow engineer", "servicenow administrator",
                   "servicenow consultant", "servicenow architect"],
        "related": ["itsm engineer", "salesforce developer", "workday consultant"],
        "skills": ["itsm", "itom", "cmdb", "glide", "flow designer"],
    },
    {
        "name": "workday",
        "match": ["workday"],
        "strong": ["workday consultant", "workday analyst", "workday integration consultant",
                   "workday hcm consultant"],
        "related": ["hris analyst", "servicenow consultant", "erp consultant"],
        "skills": ["hcm", "peci", "eib", "studio", "prism"],
    },
]

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "at", "to", "with",
    "job", "jobs", "role", "roles", "position", "opening", "hiring", "remote",
    "senior", "junior", "sr", "jr", "lead", "principal", "staff", "i", "ii",
    "iii", "entry", "level", "mid",
}


def _norm(text) -> str:
    if not text:
        return ""
    t = re.sub(r"[^a-z0-9+#. ]+", " ", str(text).lower())
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t and t not in STOPWORDS]


def find_families(query: str) -> List[Dict]:
    """Which role families the query belongs to. Usually one, sometimes two."""
    q = _norm(query)
    qt = set(_tokens(query))
    hits = []
    for fam in ROLE_FAMILIES:
        for m in fam["match"]:
            mn = _norm(m)
            # phrase present, or every word of a multi-word key is in the query
            if mn in q or (" " in mn and set(mn.split()).issubset(qt)):
                hits.append(fam)
                break
            if " " not in mn and mn in qt:
                hits.append(fam)
                break
    return hits


def expand_query(query: str, mode: str = "hybrid") -> Dict:
    """
    Returns the tiered expansion for a query.

    mode:
      "exact"  -> no expansion at all (core only)
      "hybrid" -> core + strong + related   (default)
      "strict" -> core + strong only, no adjacent roles
    """
    core = [t.strip() for t in (query or "").split(",") if t.strip()]
    result = {
        "query": query or "",
        "mode": mode,
        "core": core,
        "strong": [],
        "related": [],
        "skills": [],
        "families": [],
    }
    if not core or mode == "exact":
        return result

    fams = find_families(query)
    result["families"] = [f["name"] for f in fams]

    core_norm = {_norm(c) for c in core}
    strong: Set[str] = set()
    related: Set[str] = set()
    skills: Set[str] = set()

    for fam in fams:
        strong |= {s for s in fam["strong"] if _norm(s) not in core_norm}
        skills |= set(fam["skills"])
        if mode == "hybrid":
            related |= {r for r in fam["related"] if _norm(r) not in core_norm}

    result["strong"] = sorted(strong)
    result["related"] = sorted(related - strong)
    result["skills"] = sorted(skills)
    return result


def all_search_terms(expansion: Dict) -> List[Tuple[str, str]]:
    """Flat [(term, tier)] list used to widen the SQL pre-filter."""
    out = [(t, TIER_CORE) for t in expansion.get("core", [])]
    out += [(t, TIER_STRONG) for t in expansion.get("strong", [])]
    out += [(t, TIER_RELATED) for t in expansion.get("related", [])]
    return out


def classify_match(job: Dict, expansion: Dict) -> Tuple[str, float, str]:
    """
    Which tier this job matched at: (tier, weight, matched_term).
    Returns ("none", 0.0, "") when nothing matches.
    """
    hay_title = _norm(f"{job.get('title') or ''} {job.get('canonical_title') or ''}")
    hay_body = _norm(" ".join(str(job.get(f) or "") for f in
                             ("skills", "description_snippet", "company")))

    for term in expansion.get("core", []):
        tn = _norm(term)
        if tn and (tn in hay_title or set(tn.split()).issubset(set(hay_title.split()))):
            return TIER_CORE, TIER_WEIGHT[TIER_CORE], term

    for term in expansion.get("strong", []):
        tn = _norm(term)
        if tn and (tn in hay_title or set(tn.split()).issubset(set(hay_title.split()))):
            return TIER_STRONG, TIER_WEIGHT[TIER_STRONG], term

    for term in expansion.get("related", []):
        tn = _norm(term)
        if tn and (tn in hay_title or set(tn.split()).issubset(set(hay_title.split()))):
            return TIER_RELATED, TIER_WEIGHT[TIER_RELATED], term

    # Skill-based rescue: a vague title ("Engineer II") at a company whose
    # stack clearly identifies the role. Capped at RELATED -- a skill match is
    # weaker evidence than a title match and must never outrank one.
    matched_skills = [s for s in expansion.get("skills", []) if _norm(s) and _norm(s) in hay_body]
    if len(matched_skills) >= 2 and "engineer" in hay_title or len(matched_skills) >= 3:
        return TIER_RELATED, TIER_WEIGHT[TIER_RELATED] * 0.9, "+".join(matched_skills[:3])

    return "none", 0.0, ""


def build_scrape_keywords(expansion: Dict, max_terms: int = 4) -> List[str]:
    """
    Keywords to actually send to the portals. Core first, then the strongest
    synonyms -- capped, because each extra keyword is another paid scrape.
    Related/adjacent terms are deliberately NOT scraped; they only widen what
    we surface from what is already stored.
    """
    out = list(expansion.get("core", []))
    for s in expansion.get("strong", []):
        if len(out) >= max_terms:
            break
        out.append(s)
    return out[:max_terms]
