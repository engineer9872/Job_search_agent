"""
Query Planner (Component 7)
Generates an execution plan mapping search requests to the 5 methods based on portal capabilities
and health status from MethodHealthMonitor.
"""

import logging
from typing import Dict, Any, List, Optional
from pipeline.method_health import MethodHealthMonitor

logger = logging.getLogger(__name__)


class MethodPlan:
    """
    Plan definition for a single method execution.
    """

    def __init__(self, method_id: int, name: str, portals: List[str], is_fallback: bool = False):
        self.method_id = method_id
        self.name = name
        self.portals = portals
        self.is_fallback = is_fallback

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method_id": self.method_id,
            "name": self.name,
            "portals": self.portals,
            "is_fallback": self.is_fallback,
        }


class QueryPlanner:
    """
    Query Planner for 5-Method Max-Coverage Ingestion.
    """

    # Portal groupings by default method assignment
    METHOD_1_PORTALS = ["greenhouse", "lever", "workday", "smartrecruiters", "ashby"]
    METHOD_5_PORTALS = ["adzuna", "jsearch", "jooble", "arbeitnow", "the_muse", "reed", "remotive"]
    METHOD_2_PORTALS = ["naukri", "indeed", "linkedin", "glassdoor"]
    METHOD_4_PORTALS = ["linkedin", "naukri", "indeed", "glassdoor"]
    METHOD_3_PORTALS = ["naukri", "indeed", "linkedin", "glassdoor"]

    def __init__(self, health_monitor: Optional[MethodHealthMonitor] = None):
        self.health = health_monitor or MethodHealthMonitor()

    def plan_search(
        self,
        keyword: str = "developer",
        country: str = "in",
        target_portals: Optional[List[str]] = None,
    ) -> List[MethodPlan]:
        """
        Generates an ordered execution waterfall plan for the given search request.
        """
        active_portals = set(target_portals or [
            "greenhouse", "lever", "workday", "smartrecruiters", "ashby",
            "adzuna", "jsearch", "jooble", "arbeitnow", "the_muse", "reed", "remotive",
            "naukri", "indeed", "linkedin", "glassdoor",
        ])

        plans: List[MethodPlan] = []

        # 1. Method 1: Direct-ATS Capture (Always Active, Zero Risk)
        m1_targets = [p for p in self.METHOD_1_PORTALS if p in active_portals]
        if m1_targets:
            plans.append(MethodPlan(1, "Direct-ATS Capture", m1_targets))

        # 2. Method 5: Secondary Aggregator APIs (Always Active, Zero Risk)
        m5_targets = [p for p in self.METHOD_5_PORTALS if p in active_portals]
        if m5_targets:
            plans.append(MethodPlan(5, "Aggregator APIs", m5_targets))

        # 3. Method 4: Structured Data (JSON-LD Harvesting)
        m4_targets = [p for p in self.METHOD_4_PORTALS if p in active_portals]
        if m4_targets:
            plans.append(MethodPlan(4, "Structured Data (JSON-LD)", m4_targets))

        # 4. Method 2: Apify Actors (Primary for hard portals)
        m2_targets = [p for p in self.METHOD_2_PORTALS if p in active_portals]
        if m2_targets:
            plans.append(MethodPlan(2, "Apify Actors", m2_targets))

        # 5. Method 3: Self-Hosted Fallback (Activated if Method 2 is degraded/failing)
        m3_targets = []
        for p in m2_targets:
            if self.health.should_fallback("Method 2", p):
                logger.info(f"[QueryPlanner] Method 2 for portal '{p}' is degraded. Activating Method 3 fallback.")
                m3_targets.append(p)

        if m3_targets:
            plans.append(MethodPlan(3, "Self-Hosted Fallback Scraper", m3_targets, is_fallback=True))

        return plans
