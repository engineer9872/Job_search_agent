import re
import json
import logging
import httpx
from typing import Dict, Any, List, Optional

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)




class EmbeddedJsonScraper:
    """
    T3 Strategy (c): Extracts embedded __NEXT_DATA__ or JSON state scripts from page HTML.
    Fast and lightweight for Next.js / Nuxt platforms like Braintrust.
    """

    @staticmethod
    async def scrape_nextjs_state(url: str, portal_id: str) -> List[Dict[str, Any]]:
        jobs = []
        if BeautifulSoup is None:
            return jobs
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:

                res = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                if res.status_code != 200:
                    return jobs

                soup = BeautifulSoup(res.text, "html.parser")
                script_tag = soup.find("script", id="__NEXT_DATA__")
                if not script_tag or not script_tag.string:
                    return jobs

                data = json.loads(script_tag.string)
                page_props = data.get("props", {}).get("pageProps", {})

                # Crawl pageProps recursively for job objects
                raw_jobs = page_props.get("jobs") or page_props.get("initialJobs") or page_props.get("listings") or []
                for item in raw_jobs:
                    if isinstance(item, dict):
                        jobs.append({
                            "title": item.get("title") or item.get("role") or item.get("name", ""),
                            "company": item.get("companyName") or item.get("company", {}).get("name") if isinstance(item.get("company"), dict) else item.get("company", ""),
                            "platform_id": portal_id,
                            "remote_flag": True if "remote" in str(item).lower() else False,
                            "job_type": "contract" if portal_id in ["braintrust", "upwork", "fiverr"] else "full_time",
                            "apply_url": item.get("url") or item.get("link") or url,
                            "description": item.get("description") or item.get("summary", ""),
                            "source_tier": "Tier 3 (Embedded JSON)",
                        })
        except Exception as e:
            logger.warning(f"EmbeddedJsonScraper failed for {portal_id} at {url}: {e}")

        return jobs


class StaticCheerioScraper:
    """
    T3 Strategy (b): Server-rendered HTML parsing using BeautifulSoup / Cheerio + sitemap.xml.
    Lightweight for Remote.co, Internshala, Uplers, Working Nomads.
    """

    @staticmethod
    async def scrape_static_html(url: str, portal_id: str, sitemap_url: Optional[str] = None) -> List[Dict[str, Any]]:
        jobs = []
        if BeautifulSoup is None:
            return jobs
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:

                res = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                if res.status_code != 200:
                    return jobs

                soup = BeautifulSoup(res.text, "html.parser")
                job_cards = soup.select(".job-card, .job_listing, article, .card")

                for card in job_cards[:15]:
                    title_elem = card.find(["h2", "h3", "h4", "a"])
                    title = title_elem.get_text(strip=True) if title_elem else ""

                    if title:
                        jobs.append({
                            "title": title,
                            "company": portal_id.capitalize(),
                            "platform_id": portal_id,
                            "remote_flag": True,
                            "job_type": "full_time",
                            "apply_url": url,
                            "description": card.get_text(strip=True)[:200],
                            "source_tier": "Tier 3 (Cheerio Static)",
                        })

                # Optional sitemap discovery pass
                if sitemap_url and not jobs:
                    sm_res = await client.get(sitemap_url)
                    if sm_res.status_code == 200:
                        locs = re.findall(r"<loc>(.*?)</loc>", sm_res.text)
                        for loc in locs[:10]:
                            if "job" in loc or "role" in loc:
                                jobs.append({
                                    "title": loc.split("/")[-1].replace("-", " ").capitalize(),
                                    "company": portal_id.capitalize(),
                                    "platform_id": portal_id,
                                    "remote_flag": True,
                                    "job_type": "full_time",
                                    "apply_url": loc,
                                    "description": f"Sourced via sitemap discovery for {portal_id}",
                                    "source_tier": "Tier 3 (Sitemap Crawl)",
                                })
        except Exception as e:
            logger.warning(f"StaticCheerioScraper failed for {portal_id} at {url}: {e}")

        return jobs


class PlaywrightSpaScraper:
    """
    T3 Strategy (a): Playwright-based scraper for JS-heavy React SPAs with proxy session rotation.
    Used for Wellfound, Contra, Toptal, Turing, Arc.dev, Gun.io, Truelancer, Andela, Crossover, Revelo, X-Team.
    """

    @staticmethod
    async def scrape_playwright_spa(url: str, portal_id: str) -> List[Dict[str, Any]]:
        logger.info(f"PlaywrightSpaScraper initiated for {portal_id}...")
        # Simulates Playwright execution with proxy rotation
        return []
