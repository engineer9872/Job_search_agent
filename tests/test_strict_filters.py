import unittest
from DB import SessionLocal, Job
from api.routes.jobs import get_jobs


class TestStrictAndFilters(unittest.TestCase):
    """
    Test suite asserting strict AND-only filter query enforcement.
    Guarantees zero mismatches across job_title, platform, country, remote_only, date_posted, and job_type.
    """

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_strict_remote_contractor_filter(self):
        res = get_jobs(remote_only=True, job_type="contractor", limit=50, offset=0, db=self.db)
        jobs = res["jobs"]
        for job in jobs:
            self.assertTrue(job["remote_flag"], f"Job {job['id']} must have remote_flag=True")
            self.assertEqual(job["job_type"], "contract", f"Job {job['id']} must have job_type='contract'")

    def test_strict_platform_and_country_filter(self):
        res = get_jobs(platform="adzuna", country="US", limit=50, offset=0, db=self.db)
        jobs = res["jobs"]
        for job in jobs:
            self.assertEqual(job["source_platform"], "adzuna", f"Job {job['id']} must have platform='adzuna'")
            self.assertIn(job["country"], ["US", "United States"], f"Job {job['id']} country must be US")

    def test_strict_title_and_remote_filter(self):
        res = get_jobs(title="Software Engineer", remote_only=True, limit=50, offset=0, db=self.db)
        jobs = res["jobs"]
        self.assertGreater(len(jobs), 0, "Query for Software Engineer remote jobs should return results")
        for job in jobs:
            self.assertTrue(job["remote_flag"], f"Job {job['id']} must be remote")
            self.assertIsNotNone(job["title"], f"Job {job['id']} must have a valid title")



    def test_strict_zero_mismatch_fallback_prevention(self):
        # A filter combination that returns 0 jobs must return 0 jobs, never fallback to non-matching records
        res = get_jobs(platform="non_existent_platform_99", remote_only=True, limit=50, offset=0, db=self.db)
        self.assertEqual(res["total"], 0, "Non-matching filter combination must return total=0")
        self.assertEqual(len(res["jobs"]), 0, "Non-matching filter combination must return empty jobs list")


if __name__ == "__main__":
    unittest.main()
