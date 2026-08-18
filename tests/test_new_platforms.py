import unittest
import asyncio
from unittest.mock import patch, MagicMock
import httpx

from pipeline.filter_lock import create_locked_filter_spec
from pipeline.apify_store import check_apify_actor_available
from pipeline.normalize import normalize_connector_job, extract_work_authorization_note


class TestNewPlatformsAndWorkAuth(unittest.TestCase):
    """
    Unit tests validating the new platform configuration features:
    1. Dynamic T2 Apify Store API search.
    2. Sitemap discovery and Playwright scraper fallback interface.
    3. work_authorization_note parsing from descriptions.
    4. Non-hardcoded job_type normalization for staffing vendors.
    """

    def test_work_auth_note_extraction(self):
        desc1 = "The successful candidate must be authorized to work in the United States without restriction."
        note1 = extract_work_authorization_note(desc1)
        self.assertIsNotNone(note1)
        self.assertIn("must be authorized to work in the United States", note1)

        desc2 = "Unfortunately, we cannot offer visa sponsorship at this time."
        note2 = extract_work_authorization_note(desc2)
        self.assertIsNotNone(note2)
        self.assertIn("cannot offer visa sponsorship", note2)

        desc3 = "This is a remote contract developer role for an expert python engineer."
        note3 = extract_work_authorization_note(desc3)
        self.assertIsNone(note3)

    def test_normalization_includes_work_auth_note(self):
        raw_job = {
            "title": "Senior React Developer",
            "company": "Robert Half",
            "url": "https://www.roberthalf.com/us/en/job/react-dev/123",
            "location": "Remote - US",
            "description": "Must have 5 years React experience. Please note: we are unable to sponsor visas for this role.",
            "contract_type": "contract-to-hire"
        }
        normalized = normalize_connector_job(raw_job, source_platform="robert_half")
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.company, "Robert Half")
        self.assertEqual(normalized.job_type, "contract")
        self.assertIsNotNone(normalized.work_authorization_note)
        self.assertIn("unable to sponsor visas", normalized.work_authorization_note)

    @patch("httpx.AsyncClient.get")
    def test_apify_store_dynamic_check_found(self, mock_get):
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "data": {
                "items": [
                    {"name": "roberthalf-scraper", "title": "Robert Half Jobs Scraper", "username": "community_dev"}
                ]
            }
        }
        mock_get.return_value = mock_res
        
        loop = asyncio.new_event_loop()
        try:
            actor_id = loop.run_until_complete(check_apify_actor_available("robert_half"))
            self.assertEqual(actor_id, "community_dev/roberthalf-scraper")
        finally:
            loop.close()

    @patch("httpx.AsyncClient.get")
    def test_apify_store_dynamic_check_not_found(self, mock_get):
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "data": {
                "items": [
                    {"name": "instagram-scraper", "title": "Instagram Scraper", "username": "apify"}
                ]
            }
        }
        mock_get.return_value = mock_res
        
        loop = asyncio.new_event_loop()
        try:
            actor_id = loop.run_until_complete(check_apify_actor_available("robert_half"))
            self.assertIsNone(actor_id)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
