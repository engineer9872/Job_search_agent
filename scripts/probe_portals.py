#!/usr/bin/env python3
"""
PORTAL CAPABILITY PROBE
=======================
Run this ON YOUR MACHINE (not in a sandbox). It answers, with evidence, the
one question that decides the whole scraping architecture:

    For each of the 10 portals -- what can we actually get for FREE,
    without SerpApi, Apify or Firecrawl?

For every portal it checks, in order:
  1. Does a plain HTTP GET of the real SEARCH URL return 200?
     (not the homepage -- the actual keyword search page)
  2. Does that HTML contain schema.org/JobPosting JSON-LD?
     -> if yes, this portal needs NO paid API at all
  3. Does it contain server-rendered job cards (anchors to /job/ URLs)?
     -> if yes, a static parse works; no browser needed
  4. Is there a reachable sitemap.xml, and does it carry <lastmod>?
     -> if yes, freshness becomes ground truth instead of guessed
  5. What blocks us -- 403 / Cloudflare / DataDome / JS-only shell?
     -> ONLY these portals justify stealth-Playwright + proxy spend

Usage:
    pip install httpx beautifulsoup4 lxml
    python probe_portals.py
    python probe_portals.py --keyword "cloud engineer" --json out.json

Nothing is written to your database. This is read-only reconnaissance.
"""

import re
import sys
import json
import time
import argparse
from typing import Dict, Any, List, Optional

try:
    import httpx
except ImportError:
    sys.exit("pip install httpx beautifulsoup4 lxml")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip install beautifulsoup4 lxml")


# The REAL search URLs. The codebase currently builds `https://{portal_id}.com`
# -- a bare homepage with no keyword -- which is why the static/T3 tier returns
# zero for every portal. These are the URLs it should have been using.
SEARCH_URLS = {
    "linkedin": "https://www.linkedin.com/jobs/search?keywords={kw}&location={loc}",
    "indeed": "https://www.indeed.com/jobs?q={kw}&l={loc}&sort=date",
    "glassdoor": "https://www.glassdoor.com/Job/{kw_slug}-jobs-SRCH_KO0,{kw_len}.htm",
    "dice": "https://www.dice.com/jobs?q={kw}&location={loc}",
    "ziprecruiter": "https://www.ziprecruiter.com/jobs-search?search={kw}&location={loc}",
    "usajobs": "https://www.usajobs.gov/search/results/?k={kw}",
    "careerbuilder": "https://www.careerbuilder.com/jobs?keywords={kw}&location={loc}",
    "simplyhired": "https://www.simplyhired.com/search?q={kw}&l={loc}",
    "weworkremotely": "https://weworkremotely.com/remote-jobs/search?term={kw}",
    "hired": "https://hired.com/jobs?query={kw}",
}

SITEMAPS = {
    "dice": "https://www.dice.com/sitemap.xml",
    "ziprecruiter": "https://www.ziprecruiter.com/sitemap.xml",
    "careerbuilder": "https://www.careerbuilder.com/sitemap.xml",
    "simplyhired": "https://www.simplyhired.com/sitemap.xml",
    "indeed": "https://www.indeed.com/sitemap.xml",
    "glassdoor": "https://www.glassdoor.com/sitemap.xml",
    "dice_alt": "https://www.dice.com/sitemap_index.xml",
}

# Free, keyless endpoints that return the SAME portal's data without scraping.
# These are not "other portals" -- they are alternate doors into these ten.
NATIVE_FEEDS = {
    "weworkremotely": "https://weworkremotely.com/remote-jobs.rss",
    "usajobs": "https://data.usajobs.gov/api/search?Keyword={kw}&ResultsPerPage=25",
    "dice": "https://www.dice.com/jobs/rss?q={kw}",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

BLOCK_SIGNATURES = {
    "cloudflare": ["cf-ray", "just a moment", "checking your browser", "cf_chl", "__cf_bm"],
    "datadome": ["datadome", "dd_cookie", "geo.captcha-delivery.com"],
    "perimeterx": ["_px", "perimeterx", "px-captcha"],
    "akamai": ["akamai", "_abck", "ak_bmsc"],
    "incapsula": ["incap_ses", "_incapsula_", "imperva"],
    "generic_captcha": ["captcha", "are you a robot", "unusual traffic", "access denied"],
}


# A sandboxed/corporate egress proxy returns 403 for EVERY host, which would
# otherwise look identical to "the portal blocked us" and produce a completely
# false all-10-blocked result. Detect and label it instead of misreporting.
PROXY_DENY_SIGNATURES = [
    "host not in allowlist", "host_not_allowed", "egress settings",
    "blocked by proxy", "proxy denied", "not permitted by policy",
]


def detect_proxy_deny(headers: Dict[str, str], body: str) -> bool:
    if (headers or {}).get("x-deny-reason"):
        return True
    head = (body or "")[:600].lower()
    return any(sig in head for sig in PROXY_DENY_SIGNATURES)


def detect_block(status: int, headers: Dict[str, str], body: str) -> Optional[str]:
    if detect_proxy_deny(headers, body):
        return "LOCAL_PROXY_DENY"
    hay = (body[:200000] or "").lower() + " " + " ".join(
        f"{k}:{v}" for k, v in (headers or {}).items()
    ).lower()
    for vendor, sigs in BLOCK_SIGNATURES.items():
        if any(s in hay for s in sigs):
            return vendor
    if status in (403, 429):
        return f"http_{status}"
    if status >= 500:
        return f"server_{status}"
    return None


def count_jsonld_jobs(html: str) -> int:
    """Counts schema.org/JobPosting objects embedded in the page."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return 0
    found = 0
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some sites emit multiple concatenated objects or trailing commas
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if "JobPosting" in [str(x) for x in types if x]:
                    found += 1
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
    return found


def count_server_rendered_cards(html: str) -> int:
    """Anchors that look like individual job-detail links in the raw HTML."""
    patterns = [
        r'href="[^"]*/jobs?/view/[^"]*"',
        r'href="[^"]*/job-detail/[^"]*"',
        r'href="[^"]*/job/[^"]*"',
        r'href="[^"]*viewjob\?jk=[^"]*"',
        r'href="[^"]*/remote-jobs/[^"]*"',
        r'href="[^"]*jobListingId=[^"]*"',
        r'data-jk="[^"]*"',
    ]
    total = set()
    for p in patterns:
        for m in re.findall(p, html or "", flags=re.IGNORECASE):
            total.add(m)
    return len(total)


def is_js_shell(html: str, card_count: int, jsonld_count: int) -> bool:
    """Page loaded fine but contains no real content -- client-rendered SPA."""
    if card_count or jsonld_count:
        return False
    body_text = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    body_text = re.sub(r"<[^>]+>", " ", body_text)
    return len(body_text.split()) < 250


def probe_url(client: httpx.Client, url: str) -> Dict[str, Any]:
    t0 = time.time()
    try:
        r = client.get(url)
        body = r.text
        return {
            "ok": True,
            "status": r.status_code,
            "elapsed": round(time.time() - t0, 2),
            "bytes": len(body),
            "headers": dict(r.headers),
            "body": body,
        }
    except Exception as e:
        return {"ok": False, "status": None, "elapsed": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {e}", "body": "", "headers": {}}


def probe_sitemap(client: httpx.Client, url: str) -> Dict[str, Any]:
    res = probe_url(client, url)
    if not res["ok"] or res["status"] != 200:
        return {"reachable": False, "status": res.get("status"), "error": res.get("error")}
    body = res["body"]
    lastmods = len(re.findall(r"<lastmod>", body, flags=re.I))
    child_maps = len(re.findall(r"<sitemap>", body, flags=re.I))
    urls = len(re.findall(r"<url>", body, flags=re.I))
    return {
        "reachable": True,
        "status": 200,
        "is_index": child_maps > 0,
        "child_sitemaps": child_maps,
        "url_entries": urls,
        "lastmod_entries": lastmods,
        # This is the prize: lastmod is a REAL timestamp, which would replace
        # the precision-tolerance guessing in pipeline/freshness.py entirely.
        "gives_exact_freshness": lastmods > 0,
    }


def classify(result: Dict[str, Any]) -> str:
    """The single verdict that decides this portal's route."""
    if result["search"].get("blocked") == "LOCAL_PROXY_DENY":
        return ("INVALID: your own network/proxy blocked this host, not the portal. "
                "Run this script from a machine with open internet access.")
    if result["search"].get("blocked"):
        return "BLOCKED -> needs stealth browser or paid API"
    if not result["search"].get("reachable"):
        return "UNREACHABLE -> needs paid API"
    if result["search"].get("jsonld_jobs", 0) > 0:
        return "FREE: JSON-LD -> no SerpApi/Apify/Firecrawl needed"
    if result["search"].get("server_rendered_cards", 0) >= 5:
        return "FREE: static HTML -> plain httpx parse, no browser"
    if result["sitemap"].get("gives_exact_freshness"):
        return "FREE: sitemap harvest -> bulk ingest, exact lastmod dates"
    if result["search"].get("js_shell"):
        return "JS-ONLY -> needs a real browser (Playwright), not stealth necessarily"
    return "INCONCLUSIVE -> inspect manually"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="software engineer")
    ap.add_argument("--location", default="United States")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--timeout", type=float, default=25.0)
    args = ap.parse_args()

    kw = args.keyword
    results: Dict[str, Any] = {}

    print("=" * 78)
    print(f"PORTAL CAPABILITY PROBE  |  keyword='{kw}'  location='{args.location}'")
    print("=" * 78)

    # HTTP/2 matters here: several bot-detection vendors fingerprint clients
    # that speak HTTP/1.1 while claiming to be Chrome. Degrade gracefully if
    # the h2 package isn't installed rather than refusing to run.
    try:
        client_ctx = httpx.Client(
            headers=BROWSER_HEADERS, timeout=args.timeout,
            follow_redirects=True, http2=True,
        )
    except ImportError:
        print("  [note] 'h2' not installed -- falling back to HTTP/1.1. "
              "Run `pip install httpx[http2]` for a more realistic fingerprint.\n")
        client_ctx = httpx.Client(
            headers=BROWSER_HEADERS, timeout=args.timeout,
            follow_redirects=True,
        )

    with client_ctx as client:
        for pid, tmpl in SEARCH_URLS.items():
            url = tmpl.format(
                kw=httpx.QueryParams({"q": kw})["q"].replace(" ", "%20"),
                loc=args.location.replace(" ", "%20"),
                kw_slug=kw.lower().replace(" ", "-"),
                kw_len=len(kw),
            )

            print(f"\n--- {pid} ---")
            print(f"  GET {url[:96]}")

            r = probe_url(client, url)
            entry: Dict[str, Any] = {"search_url": url, "search": {}, "sitemap": {}, "feed": {}}

            if not r["ok"]:
                entry["search"] = {"reachable": False, "error": r["error"]}
                print(f"  ERROR: {r['error']}")
            else:
                body = r["body"]
                blocked = detect_block(r["status"], r["headers"], body)
                jsonld = count_jsonld_jobs(body)
                cards = count_server_rendered_cards(body)
                shell = is_js_shell(body, cards, jsonld)
                entry["search"] = {
                    "reachable": r["status"] == 200,
                    "status": r["status"],
                    "elapsed_s": r["elapsed"],
                    "bytes": r["bytes"],
                    "blocked": blocked,
                    "jsonld_jobs": jsonld,
                    "server_rendered_cards": cards,
                    "js_shell": shell,
                }
                print(f"  status={r['status']}  {r['bytes']:,}B  {r['elapsed']}s"
                      f"  blocked={blocked or 'no'}")
                print(f"  JSON-LD JobPosting objects : {jsonld}")
                print(f"  server-rendered job links  : {cards}")
                if shell:
                    print("  -> page is a JS shell (no content without a browser)")

            sm = SITEMAPS.get(pid)
            if sm:
                s = probe_sitemap(client, sm)
                entry["sitemap"] = s
                if s.get("reachable"):
                    print(f"  sitemap: OK  index={s['is_index']}  children={s['child_sitemaps']}"
                          f"  urls={s['url_entries']}  lastmod={s['lastmod_entries']}")
                else:
                    print(f"  sitemap: unreachable ({s.get('status') or s.get('error')})")

            feed = NATIVE_FEEDS.get(pid)
            if feed:
                f = probe_url(client, feed.format(kw=kw.replace(" ", "+")))
                items = len(re.findall(r"<item>|\"MatchedObjectId\"", f.get("body") or "", re.I))
                entry["feed"] = {"url": feed, "status": f.get("status"), "items": items}
                print(f"  native feed: status={f.get('status')} items={items}")

            entry["verdict"] = classify(entry)
            print(f"  VERDICT: {entry['verdict']}")
            results[pid] = entry
            time.sleep(1.2)  # be polite; do not hammer

    print("\n" + "=" * 78)
    print("SUMMARY — what each portal actually needs")
    print("=" * 78)
    free, browser, paid = [], [], []
    for pid, e in results.items():
        v = e["verdict"]
        print(f"  {pid:16s} {v}")
        if v.startswith("FREE"):
            free.append(pid)
        elif v.startswith("JS-ONLY"):
            browser.append(pid)
        else:
            paid.append(pid)

    invalid = [p for p, e in results.items() if e["verdict"].startswith("INVALID")]
    if invalid:
        print("\n  " + "!" * 70)
        print("  RESULTS ARE NOT USABLE: your network blocked "
              f"{len(invalid)}/{len(results)} hosts before any portal was reached.")
        print("  Re-run this from a machine with unrestricted internet access.")
        print("  " + "!" * 70)

    total = len(results)
    print(f"\n  FREE (no paid API)      : {len(free)}/{total}  {free}")
    print(f"  Needs a browser         : {len(browser)}/{total}  {browser}")
    print(f"  Needs paid API / stealth: {len(paid)}/{total}  {paid}")
    print(f"\n  Estimated SerpApi calls per search AFTER routing: {len(paid)} (currently 10)")
    if total:
        print(f"  Projected reduction: {round((1 - len(paid) / total) * 100)}%")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"\n  Full results written to {args.json_out}")


if __name__ == "__main__":
    main()
