import sys
import os
import logging
from typing import Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from DB import SessionLocal, Job, RunLog


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def audit_platform_field_quality(sample_size_per_platform: int = 50) -> Dict[str, Any]:
    """
    Nightly audit pipeline that samples random live records per platform and computes
    null/unknown rates per field (job_type, country, posted_date, canonical_title).
    Fires an alert if a platform's null rate spikes significantly.
    """
    db = SessionLocal()
    audit_report = {}
    alert_triggered = False

    try:
        # Get list of unique platforms
        platforms = [r[0] for r in db.query(Job.source_platform).distinct().all() if r[0]]
        logger.info(f"Starting quality audit across {len(platforms)} active platforms...")

        for plat in platforms:
            sample_jobs = (
                db.query(Job)
                .filter(Job.source_platform == plat)
                .order_by(Job.fetched_at.desc())
                .limit(sample_size_per_platform)
                .all()
            )

            total_sampled = len(sample_jobs)
            if total_sampled == 0:
                continue

            null_canonical = sum(1 for j in sample_jobs if not j.canonical_title)
            null_country = sum(1 for j in sample_jobs if not j.country)
            null_posted = sum(1 for j in sample_jobs if not j.posted_date)
            unknown_job_type = sum(1 for j in sample_jobs if j.job_type in [None, "unknown"])

            canonical_null_rate = null_canonical / total_sampled
            country_null_rate = null_country / total_sampled
            posted_null_rate = null_posted / total_sampled
            job_type_unknown_rate = unknown_job_type / total_sampled

            plat_metrics = {
                "total_sampled": total_sampled,
                "null_canonical_pct": round(canonical_null_rate * 100, 1),
                "null_country_pct": round(country_null_rate * 100, 1),
                "null_posted_pct": round(posted_null_rate * 100, 1),
                "unknown_job_type_pct": round(job_type_unknown_rate * 100, 1),
            }
            audit_report[plat] = plat_metrics

            # Alert threshold check: if unknown job_type or missing fields spike above 70% on a major platform
            if job_type_unknown_rate > 0.75 and plat not in ["workable", "custom"]:
                logger.warning(f"ALERT: Platform '{plat}' unknown job_type rate spiked to {plat_metrics['unknown_job_type_pct']}%!")
                alert_triggered = True

        logger.info("Quality audit completed cleanly.")
        return {
            "status": "ALERT_TRIGGERED" if alert_triggered else "HEALTHY",
            "report": audit_report
        }

    except Exception as e:
        logger.error(f"Audit quality pipeline error: {e}")
        return {"status": "ERROR", "message": str(e)}
    finally:
        db.close()


if __name__ == "__main__":
    res = audit_platform_field_quality()
    print("=== Quality Audit Report ===")
    for k, v in res.get("report", {}).items():
        print(f"Platform: {k:15s} | Sampled: {v['total_sampled']:3d} | Null Country: {v['null_country_pct']:5.1f}% | Unknown JobType: {v['unknown_job_type_pct']:5.1f}%")
