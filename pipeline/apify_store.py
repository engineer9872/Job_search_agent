import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN", "")


async def check_apify_actor_available(portal_id: str) -> Optional[str]:
    """
    Queries Apify Store API dynamically at runtime for matching community actors.
    Returns actor_id if available, or None to fall through to Tier 3.
    """
    if not APIFY_TOKEN:
        logger.info(f"APIFY_API_TOKEN missing; skipping Tier 2 actor check for {portal_id}.")
        return None

    known_actors = {
        "linkedin": "apify/linkedin-jobs-scraper",
        "indeed": "apify/indeed-jobs-scraper",
        "upwork": "apify/upwork-jobs-scraper",
        "freelancer": "apify/freelancer-scraper",
        "fiverr": "apify/fiverr-scraper",
        "wellfound": "apify/angellist-scraper",
    }

    if portal_id in known_actors:
        actor_id = known_actors[portal_id]
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"https://api.apify.com/v2/acts/{actor_id}?token={APIFY_TOKEN}")
                if res.status_code == 200:
                    logger.info(f"[Apify Store] Verified active actor '{actor_id}' for {portal_id}.")
                    return actor_id
        except Exception as e:
            logger.warning(f"Apify Store API check failed for {portal_id}: {e}")

    if portal_id in ["robert_half", "kforce"]:
        search_query = "robert-half" if portal_id == "robert_half" else "kforce"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"https://api.apify.com/v2/store?search={search_query}")
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("data", {}).get("items", [])
                    for item in items:
                        name = item.get("name", "").lower()
                        title = item.get("title", "").lower()
                        clean_query = search_query.replace("-", "")
                        if (clean_query in name or clean_query in title or 
                                search_query in name or search_query in title):
                            username = item.get("username")
                            act_name = item.get("name")
                            if username and act_name:
                                found_id = f"{username}/{act_name}"
                                logger.info(f"[Apify Store] Found matching actor for {portal_id}: '{found_id}'")
                                return found_id
        except Exception as e:
            logger.warning(f"Apify Store search failed for {portal_id}: {e}")
        
        logger.info(f"[Apify Store] No dedicated community actor found for {portal_id}. Relying on Tier 3.")

    return None
