"""
Method Health Monitor (Component 6)
Tracks success rate, error count, job volume, and freshness per method & portal.
Provides real-time health data to the QueryPlanner to decide whether to activate
primary methods or fall back to self-hosted scrapers.
Persists state to Scheduler/method_health.json.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

HEALTH_FILE_PATH = os.path.join(os.path.dirname(__file__), "..", "Scheduler", "method_health.json")


class MethodHealthMonitor:
    """
    Monitors 24-hour health and performance metrics per (method, portal) pair.
    """

    def __init__(self, state_file: str = HEALTH_FILE_PATH):
        self.state_file = os.path.abspath(state_file)
        self.records: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _get_key(self, method: str, portal: str) -> str:
        return f"{method.lower()}::{portal.lower()}"

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
            except Exception as e:
                logger.warning(f"[HealthMonitor] Could not load state file: {e}")
                self.records = {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, indent=2)
        except Exception as e:
            logger.warning(f"[HealthMonitor] Could not save state file: {e}")

    def record_run(
        self,
        method: str,
        portal: str,
        success: bool,
        jobs_found: int = 0,
        duration_ms: float = 0.0,
        error_msg: Optional[str] = None,
    ):
        """
        Records the outcome of a single method run for a given portal.
        """
        key = self._get_key(method, portal)
        now_iso = datetime.now(timezone.utc).isoformat()

        if key not in self.records:
            self.records[key] = {
                "method": method,
                "portal": portal,
                "total_runs": 0,
                "successful_runs": 0,
                "failed_runs": 0,
                "total_jobs_found": 0,
                "last_run_time": None,
                "last_success_time": None,
                "last_error": None,
                "recent_history": [],
            }

        rec = self.records[key]
        rec["total_runs"] += 1
        rec["last_run_time"] = now_iso

        if success and jobs_found > 0:
            rec["successful_runs"] += 1
            rec["total_jobs_found"] += jobs_found
            rec["last_success_time"] = now_iso
        else:
            rec["failed_runs"] += 1
            if error_msg:
                rec["last_error"] = error_msg

        # Maintain 20 most recent run records
        rec["recent_history"].append({
            "timestamp": now_iso,
            "success": success and jobs_found > 0,
            "count": jobs_found,
            "duration_ms": duration_ms,
        })
        rec["recent_history"] = rec["recent_history"][-20:]

        self._save_state()

    def get_health(self, method: str, portal: str) -> Dict[str, Any]:
        """
        Returns health statistics for a given method and portal.
        """
        key = self._get_key(method, portal)
        rec = self.records.get(key)
        if not rec or not rec.get("recent_history"):
            return {
                "method": method,
                "portal": portal,
                "health_status": "UNKNOWN",
                "success_rate_recent": 1.0,
                "avg_jobs": 0,
            }

        recent = rec["recent_history"]
        successes = sum(1 for r in recent if r.get("success"))
        success_rate = successes / len(recent)
        avg_jobs = sum(r.get("count", 0) for r in recent) / len(recent)

        status = "HEALTHY"
        if success_rate < 0.3:
            status = "CRITICAL"
        elif success_rate < 0.7:
            status = "DEGRADED"

        return {
            "method": method,
            "portal": portal,
            "health_status": status,
            "success_rate_recent": round(success_rate, 2),
            "avg_jobs": round(avg_jobs, 1),
            "total_runs": rec["total_runs"],
            "last_success": rec.get("last_success_time"),
            "last_error": rec.get("last_error"),
        }

    def should_fallback(self, method: str, portal: str) -> bool:
        """
        Returns True if primary method is failing or degraded and should trigger fallback.
        """
        health = self.get_health(method, portal)
        return health["health_status"] in ["DEGRADED", "CRITICAL"]

    def get_all_health_reports(self) -> List[Dict[str, Any]]:
        """
        Returns full list of health reports for all tracked method/portal pairs.
        """
        reports = []
        for key in sorted(self.records.keys()):
            m, p = key.split("::")
            reports.append(self.get_health(m, p))
        return reports
