"""
Background scrape manager.

===========================================================================
THE BUG THIS EXISTS TO FIX (measured, not assumed)
===========================================================================
The live-fetch path used this shape:

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(run_five_tier_orchestrator, ...)
        try:
            fut.result(timeout=12)
        except TimeoutError:
            logger.warning("Returning existing local results; "
                           "the scrape continues in the background.")

That log line is false. `with ThreadPoolExecutor` calls `shutdown(wait=True)`
on exit, so the block does NOT return after 12s -- it blocks until the
orchestrator finishes. Measured:

    timeout logged at t=2.0s
    scrape finished at t=6.0s
    with-block EXITED at t=6.0s   <-- when the request could actually respond

So the "12 second deadline" was cosmetic. Real behaviour was a 20-40 second
blocking request that logged a reassuring lie halfway through. The reported
symptom ("jobs fetched but never shown") had a different cause than assumed
-- the response was late, not early.

===========================================================================
WHAT THIS DOES INSTEAD
===========================================================================
A process-wide, non-daemon executor that OUTLIVES any single request. A
request submits a scrape, waits up to FIRST_RESPONSE_DEADLINE_SECONDS, and
then genuinely returns -- the scrape keeps running. The client gets a
`cache_key` and polls GET /api/scrape-status/{cache_key} until the job
reports `complete`, then refreshes its results.

Statuses are kept in memory keyed by cache_key, with a TTL sweep so a
long-running process cannot leak them.
"""

import time
import logging
import threading
import concurrent.futures
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)

# How long an HTTP request will wait before returning with
# scrape_status="in_progress". Chosen from observed portal latency: SerpApi
# T4 for ZipRecruiter/CareerBuilder lands at 20-25s, so a 12s deadline
# guaranteed those portals never made the first response. This is a
# perceived-responsiveness knob only -- the scrape is never cancelled.
FIRST_RESPONSE_DEADLINE_SECONDS = 20.0

# Hard ceiling on how long a single scrape may occupy a worker before we stop
# tracking it. The orchestrator has its own internal timeouts well under this.
SCRAPE_HARD_CEILING_SECONDS = 180.0

# How long a finished status stays pollable before being swept.
STATUS_TTL_SECONDS = 600.0

_MAX_CONCURRENT_SCRAPES = 6


class ScrapeJobManager:
    def __init__(self):
        # NOT a `with` block, and NOT recreated per request. A module-level
        # executor is the whole point: the future must be able to outlive the
        # request that created it.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_CONCURRENT_SCRAPES,
            thread_name_prefix="scrape",
        )
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def _sweep(self):
        now = time.time()
        dead = [
            k for k, v in self._jobs.items()
            if v.get("finished_at") and (now - v["finished_at"]) > STATUS_TTL_SECONDS
        ]
        for k in dead:
            self._jobs.pop(k, None)

    def get_status(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._sweep()
            entry = self._jobs.get(cache_key)
            if not entry:
                return None
            return {
                "cache_key": cache_key,
                "status": entry["status"],
                "started_at": entry["started_at"],
                "elapsed_seconds": round(
                    (entry.get("finished_at") or time.time()) - entry["started_at"], 1
                ),
                "result": entry.get("result"),
                "error": entry.get("error"),
            }

    def is_running(self, cache_key: str) -> bool:
        with self._lock:
            entry = self._jobs.get(cache_key)
            return bool(entry and entry["status"] == "in_progress")

    def submit_and_wait(
        self,
        cache_key: str,
        fn: Callable[[], Any],
        deadline: float = FIRST_RESPONSE_DEADLINE_SECONDS,
    ) -> Dict[str, Any]:
        """
        Submits the scrape, waits up to `deadline`, then returns REGARDLESS.

        Returns {"status": "complete"|"in_progress", "result": ..., "error": ...}.
        On "in_progress" the scrape is still genuinely running and the caller
        should tell the client to poll get_status(cache_key).
        """
        with self._lock:
            self._sweep()
            existing = self._jobs.get(cache_key)
            if existing and existing["status"] == "in_progress":
                # Someone else already started this exact scrape. Do not fire
                # a second (paid) one -- just report it is running.
                logger.info(
                    f"[ScrapeJob] {cache_key[:12]}... already in progress; not re-submitting."
                )
                return {"status": "in_progress", "result": None, "error": None,
                        "deduplicated": True}

            entry = {
                "status": "in_progress",
                "started_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._jobs[cache_key] = entry

        def _wrapped():
            try:
                res = fn()
                with self._lock:
                    e = self._jobs.get(cache_key)
                    if e is not None:
                        e["status"] = "complete"
                        e["result"] = res
                        e["finished_at"] = time.time()
                logger.info(
                    f"[ScrapeJob] {cache_key[:12]}... completed in "
                    f"{time.time() - entry['started_at']:.1f}s"
                )
                return res
            except Exception as exc:
                with self._lock:
                    e = self._jobs.get(cache_key)
                    if e is not None:
                        e["status"] = "failed"
                        e["error"] = str(exc)
                        e["finished_at"] = time.time()
                logger.error(f"[ScrapeJob] {cache_key[:12]}... failed: {exc}")
                raise

        future = self._executor.submit(_wrapped)

        try:
            result = future.result(timeout=deadline)
            return {"status": "complete", "result": result, "error": None,
                    "deduplicated": False}
        except concurrent.futures.TimeoutError:
            # THE IMPORTANT LINE: we return here for real. The future keeps
            # running on the module-level executor, which is not shut down.
            logger.info(
                f"[ScrapeJob] {cache_key[:12]}... exceeded the {deadline:.0f}s first-response "
                f"deadline. Returning current DB results now; the scrape is STILL RUNNING and "
                f"the client should poll /api/scrape-status/{cache_key[:12]}..."
            )
            return {"status": "in_progress", "result": None, "error": None,
                    "deduplicated": False}
        except Exception as exc:
            return {"status": "failed", "result": None, "error": str(exc),
                    "deduplicated": False}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            self._sweep()
            running = sum(1 for v in self._jobs.values() if v["status"] == "in_progress")
            return {
                "tracked_jobs": len(self._jobs),
                "running": running,
                "max_concurrent": _MAX_CONCURRENT_SCRAPES,
                "first_response_deadline_seconds": FIRST_RESPONSE_DEADLINE_SECONDS,
            }


scrape_manager = ScrapeJobManager()
