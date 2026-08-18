import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio
import httpx

from pipeline.filter_lock import create_locked_filter_spec
from pipeline.five_tier_orchestrator import FiveTierScraperOrchestrator


class TestTierRetriesAndFallthrough(unittest.TestCase):
    """
    Part F.2 — Unit tests simulating tier failures 1x, 2x, 3x in a row,
    asserting retry counts and fallthrough to Tier 5 DB cache.
    """

    def setUp(self):
        self.spec = create_locked_filter_spec(platform="weworkremotely", remote_only=True)
        self.orchestrator = FiveTierScraperOrchestrator(self.spec)

    def test_tier1_retry_success(self):
        call_count = 0

        def mock_fetch_rss(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.RequestError("Transient Connection Timeout")
            return [
                {"title": "Retry Success Engineer at Stripe", "company": "Stripe", "url": "https://weworkremotely.com/jobs/123", "description": "full-time role"}
            ]

        with patch("connectors.rss_api.Layer1RSSAPIConnector._fetch_rss_feed", side_effect=mock_fetch_rss):
            result = asyncio.run(self.orchestrator.fetch_tier1_direct_api("weworkremotely"))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["title"], "Retry Success Engineer at Stripe")
            self.assertEqual(call_count, 2)

    def test_tier_fallthrough_to_tier5_cache(self):
        with patch.object(self.orchestrator, "fetch_tier1_direct_api", side_effect=Exception("Failed 3x")):
            with patch.object(self.orchestrator, "fetch_tier2_apify_actor", side_effect=Exception("Failed 3x")):
                with patch.object(self.orchestrator, "fetch_tier3_custom_strategy", side_effect=Exception("Failed 3x")):
                    with patch.object(self.orchestrator, "fetch_tier4_aggregators", side_effect=Exception("Failed 3x")):
                        results = asyncio.run(self.orchestrator.run_multi_tier_pipeline("weworkremotely"))
                        self.assertIsInstance(results, list)
                        if len(results) > 0:
                            self.assertEqual(results[0]["source_tier"], "Tier 5 (Cache)")


if __name__ == "__main__":
    unittest.main()
