import unittest
from datetime import datetime
from pipeline.normalize import (
    normalize_connector_job,
    normalize_job_batch,
    NormalizedJob,
    detect_remote,
    strip_html,
)
from pipeline.dedup import Deduplicator, build_job_signature


class TestNormalize(unittest.TestCase):

    def test_strip_html(self):
        raw_html = "<strong>Senior</strong> &amp; <em>Lead</em> &quot;Python&quot; Developer&#39;s &nbsp; Role"
        self.assertEqual(strip_html(raw_html), 'Senior & Lead "Python" Developer\'s Role')

    def test_detect_remote(self):
        self.assertTrue(detect_remote("Software Engineer", "100% remote role", "USA"))
        self.assertTrue(detect_remote("WFH Backend Engineer", "Python developer", "NY"))
        self.assertFalse(detect_remote("Onsite Developer", "Office based in Austin", "Austin TX"))

    def test_normalize_usajobs_job(self):
        raw_payload = {
            "title": "Software Engineer",
            "company": "Department of Commerce",
            "url": "https://www.usajobs.gov/job/12345",
            "location": "Washington, DC",
            "remote": True,
            "job_type": "full_time",
            "description": "Awesome Python developer role.",
            "country_code": "US",
            "eligibility_note": "U.S. federal employment — typically requires U.S. citizenship",
        }

        job = normalize_connector_job(raw_payload, source_platform="usajobs", country="us")
        self.assertIsNotNone(job)
        self.assertEqual(job.title, "Software Engineer")
        self.assertEqual(job.company, "Department of Commerce")
        self.assertTrue(job.remote_flag)
        self.assertEqual(job.source_platform, "usajobs")

    def test_normalize_dice_job(self):
        raw_payload = {
            "title": "Lead DevOps Engineer",
            "company": "CloudOps Inc",
            "url": "https://www.dice.com/job/101",
            "location": "Seattle, WA",
            "remote": True,
            "job_type": "contract",
            "description": "Manage Kubernetes and CI/CD pipelines.",
        }
        job = normalize_connector_job(raw_payload, source_platform="dice", country="us")
        self.assertIsNotNone(job)
        self.assertEqual(job.title, "Lead DevOps Engineer")
        self.assertEqual(job.company, "CloudOps Inc")
        self.assertEqual(job.source_platform, "dice")
        self.assertEqual(job.job_type, "contract")
        self.assertTrue(job.remote_flag)


class TestDedup(unittest.TestCase):

    def setUp(self):
        self.deduplicator = Deduplicator(similarity_threshold=88.0)

    def test_exact_url_deduplication(self):
        job1 = NormalizedJob(
            title="Python Developer",
            company="Acme",
            city="New York",
            source_platform="dice",
            apply_url="https://example.com/job/1",
        )
        job2 = NormalizedJob(
            title="Different Title",
            company="Different Company",
            city="LA",
            source_platform="dice",
            apply_url="https://example.com/job/1",
        )

        unique_jobs, count = self.deduplicator.deduplicate([job1, job2])
        self.assertEqual(len(unique_jobs), 1)
        self.assertEqual(count, 1)

    def test_fuzzy_signature_deduplication(self):
        job1 = NormalizedJob(
            title="Senior Python Developer",
            company="Acme Corp",
            city="San Francisco",
            source_platform="dice",
            apply_url="https://example.com/job/101",
        )
        job2 = NormalizedJob(
            title="Sr Python Dev",
            company="Acme Corp Inc",
            city="San Francisco",
            source_platform="simplyhired",
            apply_url="https://example.com/job/102",
        )

        unique_jobs, count = self.deduplicator.deduplicate([job1, job2])
        self.assertEqual(len(unique_jobs), 1)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
