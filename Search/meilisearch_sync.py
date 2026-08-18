import logging
from typing import List, Dict, Any, Optional
from DB import SessionLocal, Job

logger = logging.getLogger(__name__)


class SearchIndexSync:
    """
    Search Index Exporter & Synchronization Engine.
    Converts database Job records into search-optimized documents.
    """

    def export_jobs_to_index_documents(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetches job records from database and transforms them into search index format.
        """
        session = SessionLocal()
        try:
            query = session.query(Job)
            if limit:
                query = query.limit(limit)
            jobs = query.all()

            documents = []
            for j in jobs:
                doc = {
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
                }
                documents.append(doc)

            logger.info(f"[SearchSync] Exported {len(documents)} job documents for search indexing.")
            return documents
        finally:
            session.close()


def sync_search_index(limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Synchronizes jobs database with search index.
    """
    syncer = SearchIndexSync()
    docs = syncer.export_jobs_to_index_documents(limit=limit)
    return {
        "status": "success",
        "total_indexed_documents": len(docs),
    }
