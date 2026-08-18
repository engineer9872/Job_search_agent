import logging
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from DB import SessionLocal, Job

logger = logging.getLogger(__name__)


def search_jobs_tool(
    query: Optional[str] = None,
    remote_only: bool = False,
    country: Optional[str] = None,
    min_salary: Optional[float] = None,
    max_salary: Optional[float] = None,
    platform: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Executes a structured search against the database jobs table.

    Returns:
        List of matching job dictionaries.
    """
    session = SessionLocal()
    try:
        q = session.query(Job)

        if query:
            search_term = f"%{query.strip()}%"
            q = q.filter(
                or_(
                    Job.title.ilike(search_term),
                    Job.company.ilike(search_term),
                    Job.city.ilike(search_term),
                    Job.description_snippet.ilike(search_term),
                )
            )

        if remote_only:
            q = q.filter(Job.remote_flag == True)

        if country:
            c_clean = country.strip().upper()
            if c_clean in ["IN", "INDIA"]:
                q = q.filter(or_(Job.country == "IN", Job.country.ilike("%India%"), Job.city.ilike("%India%")))
            else:
                q = q.filter(or_(Job.country == c_clean, Job.country.ilike(f"%{c_clean}%")))

        if min_salary is not None:
            q = q.filter(or_(Job.salary_min >= min_salary, Job.salary_max >= min_salary))

        if max_salary is not None:
            q = q.filter(or_(Job.salary_max <= max_salary, Job.salary_min <= max_salary))

        if platform and platform.lower() != "all":
            q = q.filter(Job.source_platform == platform.lower())

        jobs = q.order_by(Job.fetched_at.desc()).limit(limit).all()

        results = []
        for j in jobs:
            results.append({
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "city": j.city,
                "country": j.country,
                "salary_min": j.salary_min,
                "salary_max": j.salary_max,
                "currency": j.currency,
                "remote_flag": j.remote_flag,
                "job_type": j.job_type,
                "source_platform": j.source_platform,
                "apply_url": j.apply_url,
                "description_snippet": j.description_snippet,
                "posted_date": j.posted_date.isoformat() if j.posted_date else None,
                "recruiter_name": j.recruiter_name,
                "recruiter_email": j.recruiter_email,
                "company_contact_email": j.company_contact_email,
            })

        logger.info(f"[AgentTools] Executed search_jobs_tool. Found {len(results)} matches.")
        return results
    finally:
        session.close()


def get_job_details_tool(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves full details for a specific job by ID.
    """
    session = SessionLocal()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return None
        return {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "city": job.city,
            "country": job.country,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "currency": job.currency,
            "remote_flag": job.remote_flag,
            "job_type": job.job_type,
            "source_platform": job.source_platform,
            "apply_url": job.apply_url,
            "description_snippet": job.description_snippet,
            "posted_date": job.posted_date.isoformat() if job.posted_date else None,
            "fetched_at": job.fetched_at.isoformat() if job.fetched_at else None,
            "recruiter_name": job.recruiter_name,
            "recruiter_email": job.recruiter_email,
            "company_contact_email": job.company_contact_email,
        }
    finally:
        session.close()


def get_market_insights_tool(keyword: Optional[str] = None) -> Dict[str, Any]:
    """
    Computes market statistics (salary ranges, remote count, top hiring companies) for a keyword or domain.
    """
    session = SessionLocal()
    try:
        q = session.query(Job)
        if keyword:
            search_term = f"%{keyword.strip()}%"
            q = q.filter(
                or_(
                    Job.title.ilike(search_term),
                    Job.company.ilike(search_term),
                    Job.description_snippet.ilike(search_term),
                )
            )

        total_jobs = q.count()
        remote_count = q.filter(Job.remote_flag == True).count()

        top_companies = (
            session.query(Job.company, func.count(Job.id))
            .filter(Job.company.isnot(None))
            .group_by(Job.company)
            .order_by(func.count(Job.id).desc())
            .limit(5)
            .all()
        )

        return {
            "keyword": keyword or "all",
            "total_jobs": total_jobs,
            "remote_jobs": remote_count,
            "remote_percentage": round((remote_count / total_jobs * 100), 1) if total_jobs > 0 else 0.0,
            "top_companies": [{"company": comp, "count": count} for comp, count in top_companies],
        }
    finally:
        session.close()
