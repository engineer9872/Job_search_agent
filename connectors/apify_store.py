import os
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class Layer2ApifyStoreConnector:
    """
    Layer 2 Ingestion: Apify Store Community Actors.
    Invokes maintained Apify actors using Apify SDK (token=APIFY_API_TOKEN).
    """

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("APIFY_API_TOKEN")
        self.client = None
        if self.token:
            try:
                from apify_client import ApifyClient
                self.client = ApifyClient(token=self.token)
            except ImportError:
                logger.warning("[Layer 2] apify-client package not installed.")

    def fetch_portal_data(self, portal_config: Dict[str, Any], keyword: str = "developer", country: str = "us") -> List[Dict[str, Any]]:
        """
        Runs an Apify Store actor for the configured portal and retrieves dataset items.
        """
        portal_id = portal_config.get("id")
        layer2_cfg = portal_config.get("layer2", {})
        if not layer2_cfg.get("enabled") or not layer2_cfg.get("actor_id"):
            raise ValueError(f"Layer 2 actor_id not configured or disabled for portal '{portal_id}'")

        if not self.client:
            raise RuntimeError("Apify API Token not provided or ApifyClient initialization failed.")

        actor_id = layer2_cfg.get("actor_id")
        logger.info(f"[Layer 2] Invoking Apify Store actor '{actor_id}' for portal '{portal_id}'")

        run_input = {
          "search": f"{keyword} remote",
          "query": keyword,
          "maxResults": 20,
          "limit": 20,
          "country": country,
        }

        run = self.client.actor(actor_id).call(run_input=run_input)
        if not run or run.get("status") != "SUCCEEDED":
            raise RuntimeError(f"Apify actor '{actor_id}' run failed with status '{run.get('status')}'")

        dataset_id = run.get("defaultDatasetId")
        dataset_items = self.client.dataset(dataset_id).list_items().items

        logger.info(f"[Layer 2] Apify actor '{actor_id}' returned {len(dataset_items)} items for '{portal_id}'")

        raw_jobs = []
        for item in dataset_items:
            if isinstance(item, dict):
                title = item.get("title") or item.get("position") or item.get("jobTitle")
                if title:
                    raw_jobs.append({
                        "title": title,
                        "company": item.get("company") or item.get("companyName") or "Remote Client",
                        "url": item.get("url") or item.get("jobUrl") or item.get("link") or f"https://{portal_id}.com",
                        "location": item.get("location") or "Remote",
                        "remote": True,
                        "contract_type": str(item.get("contractType") or item.get("jobType") or "contract"),
                        "posted_date": item.get("postedAt") or item.get("postedDate") or item.get("date"),
                        "description": item.get("description") or item.get("snippet"),
                    })

        return raw_jobs
